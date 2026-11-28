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
  -- Deferred NO ACTION permits Project cleanup regardless of cascade order,
  -- while still preventing a plan from being deleted out from under history.
  plan_id uuid references public.experiment_plans(id)
    deferrable initially deferred,
  plan_version integer not null default 1 check (plan_version > 0),
  code_version integer not null default 0 check (code_version >= 0),
  -- Distinguishes an explicitly sealed empty file set from an unpinned attempt.
  files_pinned boolean not null default false,
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
  -- Deferred NO ACTION allows Experiment/Project cleanup to remove runs and
  -- history first, without letting direct code deletion cascade history away.
  generated_file_id uuid not null references public.generated_files(id)
    deferrable initially deferred,
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

-- Existing non-empty projections were already explicitly pinned. Empty legacy
-- projections remain unpinned so a queued run can still receive its first pin.
update public.execution_attempts ea
set files_pinned = true
where ea.attempt = 1
  and exists (
    select 1 from public.execution_run_files p
    where p.run_id = ea.run_id
  );

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

-- Validate and clear deferred FK events before later ALTER TABLE statements.
set constraints all immediate;

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
  -- A file set is write-once, including an explicitly empty set.
  if OLD.files_pinned and not NEW.files_pinned then
    raise exception 'execution attempt files_pinned cannot be unsealed';
  end if;

  -- The updated_at trigger is housekeeping and may touch even a sealed row.
  -- The one permitted mutation on a terminal row is its initial file-set seal;
  -- this lets late/legacy pinning distinguish [] from an unpinned attempt.
  if OLD.status in ('succeeded', 'failed', 'timeout', 'cancelled') then
    if (to_jsonb(NEW) - 'updated_at' - 'files_pinned')
        is distinct from
       (to_jsonb(OLD) - 'updated_at' - 'files_pinned') then
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
-- 6. Atomic exact file-set pinning.
--    The run lock serializes first-time attempt creation; the attempt row lock
--    serializes sealing and comparison. An empty array is a real pinned set.
-- ============================================================
-- Python's uuid5(NAMESPACE_URL, f"reproagent:execution:{run_id}:attempt:{n}")
-- is the unchanged 0-based Runner request identity. Keep SQL-created attempts
-- on that same identity without adding a fourth RPC argument.
create or replace function public.execution_attempt_request_id(
  p_run_id uuid,
  p_repair_attempt integer
)
returns uuid
language sql
immutable strict parallel safe
set search_path = public, pg_temp
as $$
  select (
    substr(v.hash, 1, 8) || '-' ||
    substr(v.hash, 9, 4) || '-5' || substr(v.hash, 14, 3) || '-' ||
    substr('89ab', mod(
      strpos('0123456789abcdef', substr(v.hash, 17, 1)) - 1, 4
    ) + 1, 1) ||
    substr(v.hash, 18, 3) || '-' || substr(v.hash, 21, 12)
  )::uuid
  from (
    select encode(digest(
      decode('6ba7b8119dad11d180b400c04fd430c8', 'hex') ||
      convert_to(
        'reproagent:execution:' || p_run_id::text || ':attempt:' || p_repair_attempt::text,
        'UTF8'
      ),
      'sha1'
    ), 'hex') as hash
  ) v;
$$;

revoke all on function public.execution_attempt_request_id(uuid, integer)
  from public, anon, authenticated;
grant execute on function public.execution_attempt_request_id(uuid, integer)
  to service_role;

create or replace function public.pin_attempt_files(
  p_run_id uuid,
  p_attempt integer,
  p_file_ids uuid[]
)
returns void
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
  v_run public.execution_runs%rowtype;
  v_attempt public.execution_attempts%rowtype;
begin
  if p_attempt is null or p_attempt < 1 then
    raise exception 'ATTEMPT_MUST_BE_POSITIVE';
  end if;
  if p_file_ids is null then
    raise exception 'ATTEMPT_FILES_REQUIRED';
  end if;

  -- Validate ownership of the run and serialize concurrent creation of the
  -- same missing attempt row. Existing attempt identity is never overwritten.
  select * into v_run from public.execution_runs where id = p_run_id for update;
  if not found then
    raise exception 'ATTEMPT_RUN_NOT_FOUND';
  end if;

  insert into public.execution_attempts (
    run_id, attempt, plan_id, plan_version, code_version, status,
    runner_request_id, runner_run_id
  )
  values (
    v_run.id, p_attempt, v_run.plan_id, coalesce(v_run.plan_version, 1),
    v_run.code_version,
    case when p_attempt = coalesce(v_run.repair_attempt, 0) + 1
      then v_run.status else 'queued' end,
    public.execution_attempt_request_id(v_run.id, p_attempt - 1),
    case when p_attempt = coalesce(v_run.repair_attempt, 0) + 1
      then v_run.runner_run_id::text end
  )
  on conflict (run_id, attempt) do nothing;

  -- This is the authoritative serialization point for both first pins and
  -- repeats. A competing RPC cannot observe an unsealed row after this lock.
  select * into v_attempt
  from public.execution_attempts
  where run_id = p_run_id and attempt = p_attempt
  for update;
  if not found then
    raise exception 'ATTEMPT_NOT_FOUND';
  end if;

  if exists (
    select 1 from unnest(p_file_ids) as pending(fid)
    group by pending.fid having count(*) > 1
  ) then
    raise exception 'ATTEMPT_FILES_DUPLICATE';
  end if;

  -- Generated code is scoped by the run's experiment. A plan pinned on an
  -- existing attempt must belong to that same context; legacy NULL plans are
  -- retained for failed pre-plan runs.
  if v_attempt.plan_id is not null and not exists (
    select 1 from public.experiment_plans p
    where p.id = v_attempt.plan_id and p.experiment_id = v_run.experiment_id
  ) then
    raise exception 'ATTEMPT_PLAN_CONTEXT_MISMATCH' using errcode = '23503';
  end if;
  if exists (
    select 1 from unnest(p_file_ids) as pending(fid)
    left join public.generated_files gf
      on gf.id = pending.fid and gf.experiment_id = v_run.experiment_id
    where gf.id is null
  ) then
    raise exception 'ATTEMPT_FILE_CONTEXT_MISMATCH' using errcode = '23503';
  end if;

  if v_attempt.files_pinned then
    if (
      select coalesce(array_agg(fid order by fid), array[]::uuid[])
      from unnest(p_file_ids) as pending(fid)
    ) is distinct from (
      select coalesce(array_agg(generated_file_id order by generated_file_id), array[]::uuid[])
      from public.execution_attempt_files
      where run_id = p_run_id and attempt = p_attempt
    ) then
      raise exception 'ATTEMPT_FILES_CONFLICT' using errcode = 'P0001';
    end if;
    return;
  end if;

  insert into public.execution_attempt_files (run_id, attempt, generated_file_id)
  select p_run_id, p_attempt, pending.fid
  from unnest(p_file_ids) as pending(fid);

  -- Seal even when the INSERT selected zero rows.
  update public.execution_attempts set files_pinned = true where id = v_attempt.id;
end;
$$;

revoke all on function public.pin_attempt_files(uuid, integer, uuid[])
  from public, anon, authenticated;
grant execute on function public.pin_attempt_files(uuid, integer, uuid[])
  to service_role;

-- ============================================================
-- 7. Atomic final-attempt projection replacement.
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
-- 8. Row Level Security: history is readable by the run's owner. All writes
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
-- 9. Service-role hardening. authenticated has read-only history access; the
--    service role is the only application writer. Cascades remain available to
--    database administrators and Run/Project cleanup paths.
-- ============================================================
revoke all on public.execution_attempts from anon, authenticated;
grant select on public.execution_attempts to authenticated;
revoke all on public.execution_attempt_files from anon, authenticated;
grant select on public.execution_attempt_files to authenticated;
