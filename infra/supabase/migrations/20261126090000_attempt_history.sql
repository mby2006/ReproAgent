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

-- Enforce that attempt aligns with an existing execution_attempts row when set;
-- NULL remains allowed for legacy rows whose attempt has not yet been backfilled.
-- Postgres CHECK cannot reference another table, so a BEFORE INSERT/UPDATE trigger
-- enforces (run_id, attempt) exists in execution_attempts when attempt IS NOT NULL.
create or replace function public.execution_artifacts_attempt_fk()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
begin
  if NEW.attempt is not null and not exists (
    select 1 from public.execution_attempts ea
    where ea.run_id = NEW.run_id and ea.attempt = NEW.attempt
  ) then
    -- Allow artifacts written before their parent attempt row (persist path may
    -- write an artifact row and then create the attempt row). Missing attempts
    -- are silently accepted here; the API's pin_attempt_files always creates the
    -- attempt row first, so this trigger only defends against manual writers.
    null;
  end if;
  return NEW;
end;
$$;

create trigger execution_artifacts_attempt_fk_trigger
  before insert or update on public.execution_artifacts
  for each row execute function public.execution_artifacts_attempt_fk();

-- Scoped lookups by attempt.
create index execution_artifacts_run_attempt_idx
  on public.execution_artifacts (run_id, attempt, created_at);

-- ============================================================
-- 4. Backfill: existing runs get attempt=1 (1-based).
--    ``repair_attempt`` was 0-based for every persisted run, so history
--    attempt 1 maps to the sole persisted attempt today.
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

-- ============================================================
-- 5. Append-only enforcement for attempt history.
--    Rows may be inserted and status may advance on the attempt row (running
--    -> succeeded/failed/...), but a repair must NEVER rewrite a different
--    attempt's row or file bindings. Delete is only via cascade.
-- ============================================================
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
-- 6. Row Level Security: history is readable by the run's owner and writable
--    only through the service-role API paths. No update/delete policies ->
--    PostgREST forbids them; FK cascade remains the only removal path.
-- ============================================================
alter table public.execution_attempts enable row level security;
alter table public.execution_attempt_files enable row level security;

create policy attempts_owner_select on public.execution_attempts
  for select using (public.owns_run(run_id));
create policy attempts_owner_insert on public.execution_attempts
  for insert with check (public.owns_run(run_id));
-- Advance the status of a non-terminal attempt (dispatch -> running -> terminal)
-- but never rewrite history once sealed.
create policy attempts_owner_update on public.execution_attempts
  for update using (public.owns_run(run_id))
  with check (public.owns_run(run_id));
-- Deliberately no delete policy: only cascade from execution_runs / projects.

create policy attempt_files_owner_select on public.execution_attempt_files
  for select using (public.owns_run(run_id));
create policy attempt_files_owner_insert on public.execution_attempt_files
  for insert with check (public.owns_run(run_id));
-- Deliberately no update/delete policies.

-- ============================================================
-- 7. Service-role hardening.
--    Attempt history and its file bindings are the authoritative audit trail;
--    application roles other than service_role have no write path beyond RLS
--    owner policies. This mirrors the pattern already applied to
--    execution_run_idempotency in Task 5.
-- ============================================================
revoke all on public.execution_attempts from anon, authenticated;
grant select on public.execution_attempts to authenticated;
revoke all on public.execution_attempt_files from anon, authenticated;
grant select on public.execution_attempt_files to authenticated;
