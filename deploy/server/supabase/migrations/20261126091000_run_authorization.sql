-- Task 5: Atomic latest-plan authorization, approval readiness, and idempotent run creation.
-- The database becomes the final authorization boundary for run creation.
-- Replaces the old 3-arg create_execution_run with a 6-arg version that validates:
--   1. Actor owns the experiment
--   2. Plan is the latest version (serialized via experiment row lock)
--   3. Plan is approved and passes approval readiness
--   4. No other active run exists
--   5. Idempotency via execution_run_idempotency table

-- ============================================================
-- 1. Drop the old 3-arg RPC (must happen before creating the 6-arg version)
-- ============================================================
drop function if exists public.create_execution_run(uuid, uuid, integer);

-- ============================================================
-- 2. Idempotency table
-- ============================================================
create table public.execution_run_idempotency (
  experiment_id uuid not null references public.experiments(id) on delete cascade,
  actor_id uuid not null references auth.users(id) on delete cascade,
  idempotency_key text not null,
  request_fingerprint text not null,
  run_id uuid not null references public.execution_runs(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (experiment_id, actor_id, idempotency_key)
);

create index execution_run_idempotency_fingerprint_idx
  on public.execution_run_idempotency (request_fingerprint);

alter table public.execution_run_idempotency enable row level security;
revoke all on public.execution_run_idempotency from anon, authenticated;
grant all on public.execution_run_idempotency to service_role;

-- ============================================================
-- 3. Partial unique index: at most one active run per experiment
--    Check for existing duplicates before creating (fail loudly, never delete).
-- ============================================================
do $$
begin
  if exists (
    select experiment_id from public.execution_runs
    where status in ('queued', 'dispatching', 'running')
    group by experiment_id
    having count(*) > 1
  ) then
    raise exception 'Cannot create unique index: multiple active runs exist for the same experiment. Resolve manually.'
      using errcode = 'check_violation';
  end if;
end;
$$;

create unique index execution_runs_one_active_per_experiment
  on public.execution_runs(experiment_id)
  where status in ('queued', 'dispatching', 'running');

-- ============================================================
-- 4. Serialize plan inserts against run authorization via experiment row lock.
--    A BEFORE INSERT trigger on experiment_plans acquires the experiment FOR UPDATE
--    lock, so concurrent plan insert and run creation use the same serialization point.
-- ============================================================
create or replace function public.experiment_plans_serialize_insert()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
begin
  perform 1 from public.experiments where id = NEW.experiment_id for update;
  return NEW;
end;
$$;

create trigger experiment_plans_serialize_insert_trigger
  before insert on public.experiment_plans
  for each row execute function public.experiment_plans_serialize_insert();

-- ============================================================
-- 5. New 6-arg create_execution_run RPC
-- ============================================================
create or replace function public.create_execution_run(
  p_experiment_id uuid,
  p_plan_id uuid,
  p_plan_version integer,
  p_actor_id uuid,
  p_idempotency_key text,
  p_request_fingerprint text
)
returns uuid
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
  v_run_id uuid;
  v_plan_row record;
  v_max_version integer;
  v_config jsonb;
  v_target_metric text;
  v_review_mode text;
  v_delegated_by uuid;
  v_delegated_at timestamptz;
  v_assumptions jsonb;
  v_blocking integer;
begin
  -- Step 1: Verify actor owns the experiment (experiment -> project -> user).
  if not exists (
    select 1 from public.experiments e
    join public.projects p on p.id = e.project_id
    where e.id = p_experiment_id and p.user_id = p_actor_id
  ) then
    raise exception 'FORBIDDEN'
      using errcode = '42501';
  end if;

  -- Step 2: Lock the experiment row and verify plan is the latest version.
  select max(ep.version) into v_max_version
  from public.experiment_plans ep
  where ep.experiment_id = p_experiment_id;

  -- Lock experiment (for serialization with concurrent plan inserts and run creation).
  perform 1 from public.experiments where id = p_experiment_id for update;

  -- Re-read max version after acquiring lock.
  select max(ep.version) into v_max_version
  from public.experiment_plans ep
  where ep.experiment_id = p_experiment_id;

  if p_plan_version is distinct from v_max_version then
    raise exception 'STALE_PLAN_APPROVAL'
      using errcode = 'P0001';
  end if;

  -- Step 3: Load the plan row.
  select ep.id, ep.approved_at, ep.config_json
    into v_plan_row
  from public.experiment_plans ep
  where ep.id = p_plan_id
    and ep.experiment_id = p_experiment_id
    and ep.version = p_plan_version;

  if not found then
    raise exception 'STALE_PLAN_APPROVAL'
      using errcode = 'P0001';
  end if;

  if v_plan_row.approved_at is null then
    raise exception 'PLAN_NOT_APPROVED'
      using errcode = 'P0001';
  end if;

  v_config := v_plan_row.config_json;

  -- Step 4: Approval readiness check (replicate ExperimentPlan.approval_ready).

  -- 4a: Target metric must be in supported set.
  v_target_metric := lower(
    replace(trim(v_config->'evaluation_config'->>'target_metric'), '_', ' ')
  );
  if v_target_metric not in (
    'accuracy', 'test accuracy', 'top1 accuracy', 'top-1 accuracy',
    'error', 'error rate', 'test error', 'test error rate', 'misclassification rate',
    'loss', 'test loss', 'cross entropy loss'
  ) then
    raise exception 'PLAN_NOT_READY'
      using errcode = 'P0001';
  end if;

  -- 4b: Review mode determines blocking checks.
  v_review_mode := coalesce(v_config->>'review_mode', 'assisted');
  v_assumptions := coalesce(v_config->'assumptions', '[]'::jsonb);

  if v_review_mode = 'delegated' then
    -- Delegated: delegated_at and delegated_by must exist, actor must match.
    v_delegated_by := (v_config->>'delegated_by')::uuid;
    v_delegated_at := (v_config->>'delegated_at')::timestamptz;

    if v_delegated_by is null or v_delegated_at is null then
      raise exception 'PLAN_NOT_READY'
        using errcode = 'P0001';
    end if;

    if v_delegated_by != p_actor_id then
      raise exception 'DELEGATION_ACTOR_MISMATCH'
        using errcode = 'P0001';
    end if;

    -- For delegated mode: ALL non-info required assumptions must be
    -- human_confirmed=true OR confirmed_by='AUTO'.
    select count(*) into v_blocking
    from jsonb_array_elements(v_assumptions) as a
    where (a->>'requires_confirmation')::boolean = true
      and (a->>'risk_tier') is distinct from 'info'
      and coalesce((a->>'human_confirmed')::boolean, false) = false
      and a->>'confirmed_by' is distinct from 'auto';

    if v_blocking > 0 then
      raise exception 'PLAN_NOT_READY'
        using errcode = 'P0001';
    end if;
  else
    -- Non-delegated (assisted/manual): only critical-tier unconfirmed block.
    select count(*) into v_blocking
    from jsonb_array_elements(v_assumptions) as a
    where (a->>'requires_confirmation')::boolean = true
      and coalesce((a->>'human_confirmed')::boolean, false) = false
      and a->>'risk_tier' = 'critical';

    if v_blocking > 0 then
      raise exception 'PLAN_NOT_READY'
        using errcode = 'P0001';
    end if;
  end if;

  -- Step 5: Idempotency check (before active-run check so completed runs return original).
  select ir.run_id into v_run_id
  from public.execution_run_idempotency ir
  where ir.experiment_id = p_experiment_id
    and ir.actor_id = p_actor_id
    and ir.idempotency_key = p_idempotency_key;

  if found then
    -- Same key exists: verify fingerprint matches.
    select ir.run_id into v_run_id
    from public.execution_run_idempotency ir
    where ir.experiment_id = p_experiment_id
      and ir.actor_id = p_actor_id
      and ir.idempotency_key = p_idempotency_key
      and ir.request_fingerprint = p_request_fingerprint;

    if found then
      return v_run_id;
    end if;

    raise exception 'IDEMPOTENCY_CONFLICT'
      using errcode = 'P0001';
  end if;

  -- Step 6: Active run check.
  select er.id into v_run_id
  from public.execution_runs er
  where er.experiment_id = p_experiment_id
    and er.status in ('queued', 'dispatching', 'running')
  limit 1;

  if found then
    raise exception 'ACTIVE_RUN_EXISTS'
      using errcode = 'P0001';
  end if;

  -- Step 7: Insert run, outbox, update experiment status, record idempotency.
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

  insert into public.execution_run_idempotency (
    experiment_id, actor_id, idempotency_key, request_fingerprint, run_id
  )
  values (
    p_experiment_id, p_actor_id, p_idempotency_key, p_request_fingerprint, v_run_id
  );

  return v_run_id;
end;
$$;

revoke all on function public.create_execution_run(uuid, uuid, integer, uuid, text, text)
  from public, anon, authenticated;
grant execute on function public.create_execution_run(uuid, uuid, integer, uuid, text, text)
  to service_role;
