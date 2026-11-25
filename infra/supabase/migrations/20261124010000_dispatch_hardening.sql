-- Durable execution-run dispatch: transactional outbox + idempotent workflow identity.

-- Zero denotes a newly queued row that has never had generated code dispatched.
-- Generated and executed code versions remain strictly positive.
alter table public.execution_runs
  drop constraint execution_runs_code_version_check;
alter table public.execution_runs
  add constraint execution_runs_code_version_check
  check (code_version >= 0);

alter table public.execution_runs
  drop constraint execution_runs_status_check;
alter table public.execution_runs
  add constraint execution_runs_status_check
  check (status in ('queued', 'dispatching', 'running', 'succeeded', 'failed', 'timeout', 'cancelled'));

-- A run owns at most one workflow. The worker also derives a deterministic UUID,
-- but this constraint is the final guard against concurrent or legacy writers.
create unique index workflows_run_id_unique
  on public.workflows (run_id)
  where run_id is not null;

create table public.job_outbox (
  id uuid primary key default gen_random_uuid(),
  job_type text not null,
  entity_id uuid not null,
  job_id text not null unique,
  status text not null default 'pending'
    check (status in ('pending', 'enqueued', 'failed')),
  attempts integer not null default 0 check (attempts >= 0),
  last_error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index job_outbox_pending_idx
  on public.job_outbox (status, created_at);

create trigger job_outbox_set_updated_at
  before update on public.job_outbox
  for each row execute function public.set_updated_at();

alter table public.job_outbox enable row level security;
revoke all on public.job_outbox from anon, authenticated;
grant all on public.job_outbox to service_role;

-- Service-role-only RPC: run, outbox intent, and experiment state commit together.
-- Validate the pinned plan again so a concurrent plan edit cannot create a run
-- against a different experiment or version after the API's ownership checks.
create or replace function public.create_execution_run(
  p_experiment_id uuid,
  p_plan_id uuid,
  p_plan_version integer
)
returns uuid
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
  v_run_id uuid;
begin
  if not exists (
    select 1 from public.experiments where id = p_experiment_id
  ) then
    raise exception 'Experiment % not found', p_experiment_id
      using errcode = 'foreign_key_violation';
  end if;

  if not exists (
    select 1
    from public.experiment_plans
    where id = p_plan_id
      and experiment_id = p_experiment_id
      and version = p_plan_version
  ) then
    raise exception 'Plan % version % does not belong to experiment %',
      p_plan_id, p_plan_version, p_experiment_id
      using errcode = 'foreign_key_violation';
  end if;

  insert into public.execution_runs (
    experiment_id, plan_id, code_version, plan_version, status, repair_attempt
  )
  values (
    p_experiment_id, p_plan_id, 0, p_plan_version, 'queued', 0
  )
  returning id into v_run_id;

  insert into public.job_outbox (
    job_type, entity_id, job_id, status
  )
  values (
    'run_experiment_task', v_run_id, 'run-experiment:' || v_run_id, 'pending'
  );

  update public.experiments
  set status = 'running'
  where id = p_experiment_id;

  return v_run_id;
end;
$$;

revoke all on function public.create_execution_run(uuid, uuid, integer)
  from public, anon, authenticated;
grant execute on function public.create_execution_run(uuid, uuid, integer)
  to service_role;

-- Finalize a nonterminal run and mirror its status only while it is the latest
-- run for the experiment. A stale workflow must never overwrite a newer run's
-- active or terminal experiment state.
create or replace function public.reconcile_execution_run(
  p_run_id uuid,
  p_status text,
  p_error text
)
returns boolean
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
  v_experiment_id uuid;
  v_created_at timestamptz;
begin
  select experiment_id, created_at
    into v_experiment_id, v_created_at
  from public.execution_runs
  where id = p_run_id
  for update;

  if not found then
    return false;
  end if;

  update public.execution_runs
  set status = p_status,
      finished_at = now(),
      updated_at = now(),
      error = p_error
  where id = p_run_id
    and status in ('queued', 'dispatching', 'running');

  if not found then
    return false;
  end if;

  -- Serialize with create_execution_run's experiment update. The following
  -- query then observes any newer run committed while this lock was awaited.
  perform 1 from public.experiments
  where id = v_experiment_id
  for update;

  if not exists (
    select 1
    from public.execution_runs
    where experiment_id = v_experiment_id
      and (created_at, id) > (v_created_at, p_run_id)
  ) then
    update public.experiments
    set status = case when p_status = 'timeout' then 'failed' else p_status end
    where id = v_experiment_id;
  end if;

  return true;
end;
$$;

revoke all on function public.reconcile_execution_run(uuid, text, text)
  from public, anon, authenticated;
grant execute on function public.reconcile_execution_run(uuid, text, text)
  to service_role;

-- Attempt-scoped finalization protects a newer repair attempt's audit fields.
-- require_latest additionally leaves an older run untouched for post-run audit
-- consumers while still allowing the workflow itself to finish.
create or replace function public.reconcile_execution_run(
  p_run_id uuid,
  p_status text,
  p_error text,
  p_repair_attempt integer,
  p_require_latest boolean
)
returns boolean
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
  v_experiment_id uuid;
  v_created_at timestamptz;
  v_status text;
  v_repair_attempt integer;
begin
  select experiment_id, created_at, status, repair_attempt
    into v_experiment_id, v_created_at, v_status, v_repair_attempt
  from public.execution_runs
  where id = p_run_id
  for update;

  if not found then
    return false;
  end if;

  if v_repair_attempt <> p_repair_attempt then
    return false;
  end if;

  if p_require_latest and exists (
    select 1
    from public.execution_runs
    where experiment_id = v_experiment_id
      and (created_at, id) > (v_created_at, p_run_id)
  ) then
    return false;
  end if;

  -- Cancellation reconciliation is idempotent: an already-cancelled latest
  -- row may repair its experiment mirror after a partial earlier write.
  if not (v_status = 'cancelled' and p_status = 'cancelled') then
    update public.execution_runs
    set status = p_status,
        finished_at = now(),
        updated_at = now(),
        error = p_error
    where id = p_run_id
      and repair_attempt = p_repair_attempt
      and status in ('queued', 'dispatching', 'running');

    if not found then
      return false;
    end if;
  end if;

  perform 1 from public.experiments
  where id = v_experiment_id
  for update;

  if not exists (
    select 1
    from public.execution_runs
    where experiment_id = v_experiment_id
      and (created_at, id) > (v_created_at, p_run_id)
  ) then
    update public.experiments
    set status = case when p_status = 'timeout' then 'failed' else p_status end
    where id = v_experiment_id;
  end if;

  return true;
end;
$$;

revoke all on function public.reconcile_execution_run(uuid, text, text, integer, boolean)
  from public, anon, authenticated;
grant execute on function public.reconcile_execution_run(uuid, text, text, integer, boolean)
  to service_role;
