-- Task 7: append-only execution attempt history.
--
-- Before this migration, each repair/OOM retry mutated ``execution_runs.repair_attempt``
-- in place and re-pinned ``execution_run_files`` -- so the second attempt silently
-- erased the first attempt's file bindings. This migration introduces an authoritative,
-- append-only history: one row per dispatch/persist cycle in ``execution_attempts``,
-- each with its own ``execution_attempt_files`` bindings, plus a nullable ``attempt``
-- column on ``execution_artifacts`` so a per-attempt artifact can be selected without
-- guessing from the storage path.
--
-- ``execution_runs`` and ``execution_run_files`` may still mirror the *latest* attempt
-- as a projection, but the attempts tables are the authoritative record. Legacy rows
-- (whose artifact storage path uses the old 0-based ``attempt-0`` format) are backfilled
-- to ``attempt=1`` WITHOUT renaming the underlying files -- readers continue to use
-- ``storage_path`` verbatim.
--
-- Numbering rule (do not regress):
--   ``execution_attempts.attempt`` is 1-based:  attempt = repair_attempt + 1
--   ``execution_request_id(run_id, repair_attempt)`` (Runner idempotency identity)
--   remains 0-based and unchanged.

-- ============================================================
-- 1. execution_attempts
-- ============================================================
create table public.execution_attempts (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.execution_runs(id) on delete cascade,
  attempt integer not null check (attempt > 0),
  -- Legacy failed runs may lack a plan_id; new runs always populate it.
  plan_id uuid references public.experiment_plans(id),
  plan_version integer not null default 1 check (plan_version > 0),
  code_version integer not null default 0 check (code_version >= 0),
  status text not null default 'queued'
    check (status in ('queued', 'dispatching', 'running', 'succeeded',
                      'failed', 'timeout', 'cancelled')),
  runner_request_id uuid,
  runner_run_id text,
  dispatch_started_at timestamptz,
  runner_accepted_at timestamptz,
  finished_at timestamptz,
  timing_json jsonb not null default '{}'::jsonb,
  error_code text,
  error_message text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  -- A repair never reuses an ordinal and an attempt never forks into two rows.
  unique (run_id, attempt)
);

-- Foreign-key target for execution_attempt_files (composite).
create unique index execution_attempts_run_attempt_uk
  on public.execution_attempts (run_id, attempt);

-- Query patterns the API needs:
--   * list_attempts: (run_id, attempt) -- covered by the unique index above.
--   * terminal-state guards: (run_id, attempt, status).
--   * chronological listing: (run_id, attempt, created_at).
create index execution_attempts_run_status_idx
  on public.execution_attempts (run_id, attempt, status);
create index execution_attempts_run_created_idx
  on public.execution_attempts (run_id, attempt, created_at);

create trigger execution_attempts_set_updated_at
  before update on public.execution_attempts
  for each row execute function public.set_updated_at();

-- ============================================================
-- 2. execution_attempt_files
-- ============================================================
create table public.execution_attempt_files (
  run_id uuid not null references public.execution_runs(id) on delete cascade,
  attempt integer not null,
  generated_file_id uuid not null references public.generated_files(id),
  primary key (run_id, attempt, generated_file_id),
  foreign key (run_id, attempt)
    references public.execution_attempts (run_id, attempt) on delete cascade
);

-- Reverse index: which attempts pinned a given generated file.
create index execution_attempt_files_file_idx
  on public.execution_attempt_files (generated_file_id);

-- ============================================================
-- 3. execution_artifacts: nullable per-attempt ordinal
-- ============================================================
alter table public.execution_artifacts
  add column attempt integer;

-- Null remains allowed for legacy rows whose attempt has not yet been backfilled.
-- The composite FK below is added only after every artifact has a parent attempt.

-- Scoped lookups by attempt.
create index execution_artifacts_run_attempt_idx
  on public.execution_artifacts (run_id, attempt, created_at);

-- ============================================================
-- 4. Backfill existing history before enforcing artifact parentage.
--    Every existing run gets attempt=1. Artifact metadata is then mapped from
--    its actual 0-based value, and any additional parent attempts are created.
-- ============================================================
insert into public.execution_attempts (
  run_id, attempt, plan_id, plan_version, code_version, status,
  runner_run_id, dispatch_started_at, finished_at, created_at
)
select
  r.id,
  1,
  r.plan_id,
  coalesce(r.plan_version, 1),
  coalesce(r.code_version, 0),
  r.status,
  r.runner_run_id::text,
  r.started_at,
  r.finished_at,
  r.created_at
from public.execution_runs r
where not exists (
  select 1 from public.execution_attempts ea
  where ea.run_id = r.id and ea.attempt = 1
);

-- Mirror the existing final-projection bindings into history for attempt=1.
insert into public.execution_attempt_files (run_id, attempt, generated_file_id)
select p.run_id, 1, p.generated_file_id
from public.execution_run_files p
where exists (
  select 1 from public.execution_attempts ea
  where ea.run_id = p.run_id and ea.attempt = 1
)
on conflict (run_id, attempt, generated_file_id) do nothing;

-- Legacy artifact rows (attempt IS NULL) belong to attempt=1. Their on-disk
-- storage_path stays at ``{run_id}/attempt-0/...``; readers must use
-- ``storage_path`` verbatim and never re-name the underlying objects.
-- Rows whose metadata_json.attempt was already recorded (from an in-flight
-- repair in the previous 0-based schema) map back via ``+ 1``.
update public.execution_artifacts
set attempt = 1
where attempt is null
  and (metadata_json is null
       or (metadata_json->>'attempt') is null
       or (metadata_json->>'attempt')::text = '0');

update public.execution_artifacts
set attempt = ((metadata_json->>'attempt')::integer + 1)
where attempt is null
  and metadata_json is not null
  and (metadata_json->>'attempt') is not null
  and (metadata_json->>'attempt')::text <> '0';

-- Artifacts are evidence that their attempt existed. Backfill EVERY distinct
-- mapped (run_id, attempt), not just attempt=1, before enforcing the FK. Without
-- this step an in-flight repair could leave attempt-2 artifacts with no parent.
insert into public.execution_attempts (
  run_id, attempt, plan_id, plan_version, code_version, status,
  runner_run_id, dispatch_started_at, finished_at, created_at
)
select
  r.id,
  a.attempt,
  r.plan_id,
  coalesce(r.plan_version, 1),
  coalesce(r.code_version, 0),
  case when a.attempt = r.repair_attempt + 1 then r.status else 'queued' end,
  case when a.attempt = r.repair_attempt + 1 then r.runner_run_id::text end,
  case when a.attempt = r.repair_attempt + 1 then r.started_at end,
  case when a.attempt = r.repair_attempt + 1 then r.finished_at end,
  min(a.created_at)
from public.execution_artifacts a
join public.execution_runs r on r.id = a.run_id
where a.attempt is not null
group by r.id, a.attempt
on conflict (run_id, attempt) do nothing;

-- NULL remains valid for untagged legacy rows; every non-NULL artifact attempt
-- must now have a real parent. UPDATE is covered as well as INSERT.
alter table public.execution_artifacts
  add constraint execution_artifacts_run_attempt_fk
  foreign key (run_id, attempt)
  references public.execution_attempts (run_id, attempt);

-- ============================================================
-- 5. Append-only enforcement for attempt history.
--    Direct DELETE is blocked, while FK cascades from Run/Project cleanup are
--    allowed (cascade triggers run at pg_trigger_depth() > 0). Open attempts may
--    advance lifecycle/error/timestamp fields, but identity is immutable and
--    plan/code pins are fixed once running. A sealed terminal row permits only
--    an idempotent no-op.
-- ============================================================
create or replace function public.execution_attempt_history_no_delete()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
begin
  -- Top-level DELETE triggers run at depth 1; FK cascade triggers nest deeper.
  if pg_trigger_depth() <= 1 then
    raise exception 'execution attempt history is append-only; delete is allowed only by cascade';
  end if;
  return OLD;
end;
$$;

create trigger execution_attempts_no_delete
  before delete on public.execution_attempts
  for each row execute function public.execution_attempt_history_no_delete();

create trigger execution_attempt_files_no_delete
  before delete on public.execution_attempt_files
  for each row execute function public.execution_attempt_history_no_delete();

create or replace function public.execution_attempts_append_only()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
  v_old_rank integer;
  v_new_rank integer;
begin
  -- The updated_at trigger is housekeeping and may touch even a sealed row.
  if OLD.status in ('succeeded', 'failed', 'timeout', 'cancelled') then
    if (to_jsonb(NEW) - 'updated_at') is distinct from (to_jsonb(OLD) - 'updated_at') then
      raise exception 'terminal execution attempt is sealed and cannot be rewritten';
    end if;
    return NEW;
  end if;

  if (NEW.id, NEW.run_id, NEW.attempt, NEW.created_at)
    is distinct from
     (OLD.id, OLD.run_id, OLD.attempt, OLD.created_at) then
    raise exception 'execution attempt identity is immutable';
  end if;

  -- A queued pin can precede code generation/projection updates. Opening it for
  -- execution binds the final plan/code versions; a running attempt cannot fork.
  if (NEW.plan_id, NEW.plan_version, NEW.code_version)
      is distinct from (OLD.plan_id, OLD.plan_version, OLD.code_version)
     and (OLD.status = 'running' or NEW.status <> 'running') then
    raise exception 'execution attempt plan/code pins cannot be rewritten';
  end if;

  v_old_rank := case OLD.status when 'queued' then 0 when 'dispatching' then 1 else 2 end;
  v_new_rank := case NEW.status when 'queued' then 0 when 'dispatching' then 1 else 2 end;
  if NEW.status <> OLD.status
     and NEW.status not in ('succeeded', 'failed', 'timeout', 'cancelled')
     and v_new_rank < v_old_rank then
    raise exception 'execution attempt status cannot regress';
  end if;

  -- Runner identities are write-once. Pin the deterministic request id before
  -- dispatch, then attach the accepted Runner run id without rewriting either.
  if OLD.runner_request_id is not null
     and NEW.runner_request_id is distinct from OLD.runner_request_id then
    raise exception 'execution attempt runner_request_id is immutable once set';
  end if;
  if OLD.runner_run_id is not null
     and NEW.runner_run_id is distinct from OLD.runner_run_id then
    raise exception 'execution attempt runner_run_id is immutable once set';
  end if;

  -- Lifecycle timestamps may be filled in, but never cleared or moved backwards.
  if OLD.dispatch_started_at is not null
     and (NEW.dispatch_started_at is null or NEW.dispatch_started_at < OLD.dispatch_started_at) then
    raise exception 'execution attempt dispatch_started_at cannot regress';
  end if;
  if OLD.runner_accepted_at is not null
     and (NEW.runner_accepted_at is null or NEW.runner_accepted_at < OLD.runner_accepted_at) then
    raise exception 'execution attempt runner_accepted_at cannot regress';
  end if;
  if OLD.finished_at is not null
     and (NEW.finished_at is null or NEW.finished_at < OLD.finished_at) then
    raise exception 'execution attempt finished_at cannot regress';
  end if;
  if NEW.finished_at is not null
     and NEW.status not in ('succeeded', 'failed', 'timeout', 'cancelled') then
    raise exception 'open execution attempt cannot have finished_at';
  end if;
  return NEW;
end;
$$;

-- Alphabetically before set_updated_at so terminal no-ops can ignore only the
-- timestamp maintained by that housekeeping trigger.
create trigger execution_attempts_append_only
  before update on public.execution_attempts
  for each row execute function public.execution_attempts_append_only();

create or replace function public.execution_attempt_files_immutable()
returns trigger
language plpgsql
as $$
begin
  raise exception
    'execution_attempt_files is append-only; bind a new attempt instead of updating';
end;
$$;

drop trigger if exists execution_attempt_files_immutable on public.execution_attempt_files;
create trigger execution_attempt_files_immutable
  before update on public.execution_attempt_files
  for each row execute function public.execution_attempt_files_immutable();

-- ============================================================
-- 6. Atomic final-attempt projection replacement.
--    History is not touched. The run lock serializes competing replacements;
--    any missing attempt/file aborts the whole transaction, including DELETE.
-- ============================================================
create or replace function public.replace_run_projection(
  p_run_id uuid,
  p_attempt integer,
  p_file_ids uuid[]
)
returns void
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
begin
  if p_attempt is null or p_attempt < 1 then
    raise exception 'projection attempt must be positive';
  end if;
  perform 1 from public.execution_runs where id = p_run_id for update;
  if not found then
    raise exception 'projection run does not exist';
  end if;
  if not exists (
    select 1 from public.execution_attempts
    where run_id = p_run_id and attempt = p_attempt
  ) then
    raise exception 'projection attempt does not belong to run';
  end if;
  if exists (
    select 1 from unnest(p_file_ids) as pending(fid)
    left join public.generated_files gf on gf.id = pending.fid
    where gf.id is null
  ) then
    raise exception 'projection contains an unknown generated file'
      using errcode = '23503';
  end if;

  delete from public.execution_run_files where run_id = p_run_id;
  insert into public.execution_run_files (run_id, generated_file_id)
  select distinct p_run_id, pending.fid
  from unnest(coalesce(p_file_ids, array[]::uuid[])) as pending(fid);
end;
$$;

revoke all on function public.replace_run_projection(uuid, integer, uuid[])
  from public, anon, authenticated;
grant execute on function public.replace_run_projection(uuid, integer, uuid[])
  to service_role;

-- ============================================================
-- 7. Row Level Security: history is readable by the run's owner. All writes
--    use the service-role API; unlike RLS alone, the triggers above also block
--    direct service-role deletes and terminal/identity rewrites.
-- ============================================================
alter table public.execution_attempts enable row level security;
alter table public.execution_attempt_files enable row level security;

create policy attempts_owner_select on public.execution_attempts
  for select using (public.owns_run(run_id));

create policy attempt_files_owner_select on public.execution_attempt_files
  for select using (public.owns_run(run_id));

-- ============================================================
-- 8. Service-role hardening. authenticated has read-only history access; the
--    service role is the only application writer. Cascades remain available to
--    database administrators and Run/Project cleanup paths.
-- ============================================================
revoke all on public.execution_attempts from anon, authenticated;
grant select on public.execution_attempts to authenticated;
revoke all on public.execution_attempt_files from anon, authenticated;
grant select on public.execution_attempt_files to authenticated;
