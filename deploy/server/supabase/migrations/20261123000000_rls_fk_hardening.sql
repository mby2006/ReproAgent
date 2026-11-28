-- Harden RLS against cross-tenant foreign keys and close audit-chain gaps.
--
-- Before this migration, INSERT/UPDATE WITH CHECK only validated the direct
-- ownership root (project_id / experiment_id / paper_id). A user could point a
-- FK column (workflows.paper_id, experiments.spec_id, execution_runs.plan_id,
-- understanding_jobs.spec_version_id, execution_run_files.generated_file_id) at
-- another tenant's row via direct PostgREST, then drive the service-role worker
-- to read that row's content. Every FK is now ownership-checked too.
--
-- Also: experiment_plans becomes non-deletable by owners (audit chain), and
-- reproduction_specs / generated_files gain BEFORE UPDATE immutability triggers
-- so even the service role cannot rewrite pinned content. DELETE is left to FK
-- cascade (a delete trigger would break experiment/user teardown).

-- ---------- additional ownership helpers (security invoker, acyclic) ----------
create or replace function public.owns_spec(s_id uuid)
returns boolean
language sql stable security invoker set search_path = public
as $$
  select exists (
    select 1 from public.reproduction_specs s
    where s.id = s_id and public.owns_paper(s.paper_id)
  )
$$;

create or replace function public.owns_plan(p_id uuid)
returns boolean
language sql stable security invoker set search_path = public
as $$
  select exists (
    select 1 from public.experiment_plans p
    where p.id = p_id and public.owns_experiment(p.experiment_id)
  )
$$;

create or replace function public.owns_generated_file(gf_id uuid)
returns boolean
language sql stable security invoker set search_path = public
as $$
  select exists (
    select 1 from public.generated_files g
    where g.id = gf_id and public.owns_experiment(g.experiment_id)
  )
$$;

-- ---------- workflows: check every FK, not just project_id ----------
drop policy if exists workflows_owner_insert on public.workflows;
create policy workflows_owner_insert on public.workflows
  for insert with check (
    public.owns_project(project_id)
    and public.owns_paper(paper_id)
    and (experiment_id is null or public.owns_experiment(experiment_id))
    and (run_id is null or public.owns_run(run_id))
    and (spec_version_id is null or public.owns_spec(spec_version_id))
    and (plan_id is null or public.owns_plan(plan_id))
  );

drop policy if exists workflows_owner_update on public.workflows;
create policy workflows_owner_update on public.workflows
  for update using (public.owns_project(project_id))
  with check (
    public.owns_project(project_id)
    and public.owns_paper(paper_id)
    and (experiment_id is null or public.owns_experiment(experiment_id))
    and (run_id is null or public.owns_run(run_id))
    and (spec_version_id is null or public.owns_spec(spec_version_id))
    and (plan_id is null or public.owns_plan(plan_id))
  );

-- ---------- experiments: paper_id / spec_id must belong to the owner ----------
drop policy if exists experiments_owner_insert on public.experiments;
create policy experiments_owner_insert on public.experiments
  for insert with check (
    public.owns_project(project_id)
    and public.owns_paper(paper_id)
    and (spec_id is null or public.owns_spec(spec_id))
  );

drop policy if exists experiments_owner_update on public.experiments;
create policy experiments_owner_update on public.experiments
  for update using (public.owns_project(project_id))
  with check (
    public.owns_project(project_id)
    and public.owns_paper(paper_id)
    and (spec_id is null or public.owns_spec(spec_id))
  );

-- ---------- execution_runs: plan_id must belong to the owner ----------
drop policy if exists runs_owner_insert on public.execution_runs;
create policy runs_owner_insert on public.execution_runs
  for insert with check (
    public.owns_experiment(experiment_id)
    and public.owns_plan(plan_id)
  );

drop policy if exists runs_owner_update on public.execution_runs;
create policy runs_owner_update on public.execution_runs
  for update using (public.owns_experiment(experiment_id))
  with check (
    public.owns_experiment(experiment_id)
    and public.owns_plan(plan_id)
  );

-- ---------- understanding_jobs: spec_version_id must belong to the owner ----------
drop policy if exists ujobs_owner_insert on public.understanding_jobs;
create policy ujobs_owner_insert on public.understanding_jobs
  for insert with check (
    public.owns_paper(paper_id)
    and (spec_version_id is null or public.owns_spec(spec_version_id))
  );

drop policy if exists ujobs_owner_update on public.understanding_jobs;
create policy ujobs_owner_update on public.understanding_jobs
  for update using (public.owns_paper(paper_id))
  with check (
    public.owns_paper(paper_id)
    and (spec_version_id is null or public.owns_spec(spec_version_id))
  );

-- ---------- execution_run_files: pinned file must belong to the owner ----------
drop policy if exists runfiles_owner_insert on public.execution_run_files;
create policy runfiles_owner_insert on public.execution_run_files
  for insert with check (
    public.owns_run(run_id)
    and public.owns_generated_file(generated_file_id)
  );

-- ---------- agent_traces: optional run_id must belong to the owner ----------
drop policy if exists traces_owner_insert on public.agent_traces;
create policy traces_owner_insert on public.agent_traces
  for insert with check (
    (
      (workflow_id is not null and public.owns_workflow(workflow_id))
      or (experiment_id is not null and public.owns_experiment(experiment_id))
    )
    and (run_id is null or public.owns_run(run_id))
  );

-- ---------- experiment_plans: owners may not delete versions (audit chain) ----------
-- FK cascade from experiments still removes plans during teardown; this only
-- blocks explicit owner DELETE through PostgREST.
drop policy if exists plans_owner_delete on public.experiment_plans;

-- ---------- append-only content: block UPDATE even for service_role ----------
create or replace function public.reproduction_specs_immutable()
returns trigger
language plpgsql
as $$
begin
  raise exception
    'reproduction_specs is append-only; insert a new version instead of updating';
end;
$$;

drop trigger if exists reproduction_specs_immutable on public.reproduction_specs;
create trigger reproduction_specs_immutable
  before update on public.reproduction_specs
  for each row execute function public.reproduction_specs_immutable();

create or replace function public.generated_files_immutable()
returns trigger
language plpgsql
as $$
begin
  raise exception
    'generated_files is append-only; insert a new version instead of updating';
end;
$$;

drop trigger if exists generated_files_immutable on public.generated_files;
create trigger generated_files_immutable
  before update on public.generated_files
  for each row execute function public.generated_files_immutable();