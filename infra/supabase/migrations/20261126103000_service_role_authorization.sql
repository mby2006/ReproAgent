-- The API's service-role client intentionally bypasses RLS, so router-level
-- actor ownership checks remain mandatory. Keep the SQL privileges explicit for
-- stock Postgres as well as Supabase instead of relying on platform defaults.
--
-- create_execution_run is SECURITY INVOKER: its ownership guard and atomic
-- authorization writes must be able to execute as service_role. These grants do
-- not change any RLS policy or authorize a foreign actor; the RPC still rejects
-- an actor who does not own the selected project.

grant select on public.projects to service_role;
grant select on public.papers to service_role;
grant select on public.reproduction_specs to service_role;
grant select, update on public.experiments to service_role;
grant select on public.experiment_plans to service_role;
grant select on public.generated_files to service_role;
grant select, insert, update on public.execution_runs to service_role;
grant select on public.execution_attempts to service_role;
grant select on public.execution_attempt_files to service_role;
grant select on public.execution_run_files to service_role;
grant select on public.reproduction_reports to service_role;
grant select on public.execution_artifacts to service_role;
grant select, update on public.workflows to service_role;
grant select on public.agent_traces to service_role;
grant insert, update on public.job_outbox to service_role;

-- Supabase enables this by default. Stock Postgres templates do not; without
-- RLS the per-user paper policies (and the default-deny runs bucket) are inert.
alter table storage.objects enable row level security;
