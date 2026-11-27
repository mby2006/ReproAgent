-- Close the Runner-result persistence race with cancellation and terminal sealing.
--
-- Storage/artifact downloads happen before this RPC. The Run row lock, matching
-- attempt guard, current-attempt artifact replacement, and audit update then run
-- in one transaction. A terminal Run is immutable to late Runner output.

create or replace function public.persist_execution_result(
  p_run_id uuid,
  p_repair_attempt integer,
  p_artifact_rows jsonb,
  p_exit_code integer,
  p_metrics_json jsonb,
  p_execution_time numeric,
  p_audit_json jsonb,
  p_stdout_path text,
  p_stderr_path text,
  p_error_category text,
  p_error text
)
returns boolean
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
  v_run public.execution_runs%rowtype;
  v_history_attempt integer;
  v_prefix text;
begin
  if p_repair_attempt is null or p_repair_attempt < 0 then
    raise exception 'RESULT_ATTEMPT_INVALID';
  end if;
  if p_artifact_rows is null or jsonb_typeof(p_artifact_rows) <> 'array' then
    raise exception 'RESULT_ARTIFACTS_REQUIRED';
  end if;

  select * into v_run
  from public.execution_runs
  where id = p_run_id
  for update;
  if not found then
    return false;
  end if;

  -- All four terminal statuses are protected, not only cancel/success.
  if v_run.status not in ('queued', 'dispatching', 'running')
     or coalesce(v_run.repair_attempt, 0) <> p_repair_attempt then
    return false;
  end if;

  v_history_attempt := p_repair_attempt + 1;
  if not exists (
    select 1 from public.execution_attempts
    where run_id = p_run_id and attempt = v_history_attempt
  ) then
    raise exception 'RESULT_ATTEMPT_NOT_FOUND';
  end if;

  v_prefix := p_run_id::text || '/attempt-' || v_history_attempt::text || '/artifacts/';

  -- Replace only this attempt's evidence. Prior attempts and their append-only
  -- attempt/file/report history are never touched. Any insert failure rolls back
  -- both this DELETE and the Run audit UPDATE.
  delete from public.execution_artifacts
  where run_id = p_run_id
    and (
      attempt = v_history_attempt
      or metadata_json->>'attempt' = v_history_attempt::text
      or storage_path like v_prefix || '%'
    );

  insert into public.execution_artifacts (
    run_id, attempt, artifact_type, storage_path, metadata_json
  )
  select
    p_run_id,
    v_history_attempt,
    pending.artifact_type,
    pending.storage_path,
    pending.metadata_json
  from jsonb_to_recordset(p_artifact_rows) as pending(
    artifact_type text,
    storage_path text,
    metadata_json jsonb
  );

  update public.execution_runs
  set
    exit_code = p_exit_code,
    metrics_json = coalesce(p_metrics_json, '{}'::jsonb),
    runtime_json = jsonb_build_object('execution_time', p_execution_time),
    audit_json = coalesce(p_audit_json, '{}'::jsonb),
    stdout_path = p_stdout_path,
    stderr_path = p_stderr_path,
    error_category = coalesce(p_error_category, error_category),
    error = coalesce(p_error, error)
  where id = p_run_id;

  return true;
end;
$$;

revoke all on function public.persist_execution_result(
  uuid, integer, jsonb, integer, jsonb, numeric, jsonb, text, text, text, text
) from public, anon, authenticated;
grant execute on function public.persist_execution_result(
  uuid, integer, jsonb, integer, jsonb, numeric, jsonb, text, text, text, text
) to service_role;

-- Stock-Postgres privileges for the production service-role client. Positive
-- workflow controls execute as this role; Supabase defaults are not assumed.
-- Append-only Card/code/attempt-file/report history receives no UPDATE/DELETE;
-- attempt lifecycle fields retain their existing guarded UPDATE path.
grant select, insert on public.projects to service_role;
grant select, insert, update, delete on public.papers to service_role;
grant select, insert on public.reproduction_specs to service_role;
grant select, insert, update on public.experiments to service_role;
grant select, insert, update on public.experiment_plans to service_role;
grant select, insert on public.generated_files to service_role;
grant select, insert, update on public.execution_runs to service_role;
grant select, insert, update on public.execution_attempts to service_role;
grant select, insert on public.execution_attempt_files to service_role;
grant select, insert, delete on public.execution_run_files to service_role;
grant select, insert on public.reproduction_reports to service_role;
grant select, insert, delete on public.execution_artifacts to service_role;
grant select, insert, update on public.workflows to service_role;
grant select, insert on public.agent_traces to service_role;
grant select, insert on public.execution_run_idempotency to service_role;

-- Card versions are append-only and artifact rows are replaced rather than
-- edited. Remove broader UPDATE privileges that stock roles may already have.
revoke update on public.reproduction_specs, public.execution_artifacts from service_role;

-- Earlier hardening migrations granted broad outbox/idempotency ACLs. Replace
-- those grants so production can enqueue/update jobs and append idempotency
-- records, but cannot truncate, rewrite, or delete those records.
revoke all on public.job_outbox from service_role;
revoke all on public.execution_run_idempotency from service_role;
grant select, insert, update on public.job_outbox to service_role;
grant select, insert on public.execution_run_idempotency to service_role;

-- Storage ACLs are platform-managed by Supabase; the real-Supabase E2E
-- validates the service-role privileges required by upload/upsert/download.
