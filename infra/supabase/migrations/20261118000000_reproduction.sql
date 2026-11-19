-- ReproAgent reproduction/execution domain (Phase 3 skeleton).
-- Ownership flows experiments.project_id -> projects.user_id, and runs/artifacts
-- /reports derive ownership through their experiment/run. Code versions are
-- pinned exactly via execution_run_files -> generated_files.id.

-- ---------- experiments ----------
create table public.experiments (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references public.projects (id) on delete cascade,
  paper_id uuid not null references public.papers (id) on delete cascade,
  spec_id uuid references public.reproduction_specs (id),
  name text not null check (char_length(name) between 1 and 200),
  status text not null default 'pending'
    check (status in ('pending', 'planning', 'running', 'succeeded', 'failed', 'cancelled')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index experiments_project_id_idx on public.experiments (project_id);
create index experiments_paper_id_idx on public.experiments (paper_id);

create trigger experiments_set_updated_at
  before update on public.experiments
  for each row execute function public.set_updated_at();

create or replace function public.owns_experiment(e_id uuid)
returns boolean
language sql stable security invoker set search_path = public
as $$
  select exists (
    select 1 from public.experiments e
    where e.id = e_id and public.owns_project(e.project_id)
  )
$$;

-- ---------- experiment_plans: versioned, approval is mutable ----------
create table public.experiment_plans (
  id uuid primary key default gen_random_uuid(),
  experiment_id uuid not null references public.experiments (id) on delete cascade,
  version integer not null check (version >= 1),
  config_json jsonb not null,
  budget_json jsonb not null default '{}'::jsonb,
  approved_at timestamptz,
  created_at timestamptz not null default now(),
  unique (experiment_id, version)
);

create index experiment_plans_experiment_version_idx
  on public.experiment_plans (experiment_id, version desc);

-- ---------- generated_files: append-only code versions ----------
create table public.generated_files (
  id uuid primary key default gen_random_uuid(),
  experiment_id uuid not null references public.experiments (id) on delete cascade,
  version integer not null check (version >= 1),
  path text not null,
  content text,
  storage_path text,
  sha256 text not null,
  change_reason text,
  related_error text,
  created_at timestamptz not null default now(),
  unique (experiment_id, version, path)
);

create index generated_files_experiment_version_idx
  on public.generated_files (experiment_id, version);

-- ---------- execution_runs ----------
create table public.execution_runs (
  id uuid primary key default gen_random_uuid(),
  experiment_id uuid not null references public.experiments (id) on delete cascade,
  plan_id uuid not null references public.experiment_plans (id),
  code_version integer not null check (code_version >= 1),
  status text not null default 'queued'
    check (status in ('queued', 'running', 'succeeded', 'failed', 'timeout', 'cancelled')),
  exit_code integer,
  metrics_json jsonb not null default '{}'::jsonb,
  runtime_json jsonb not null default '{}'::jsonb,
  stdout_path text,
  stderr_path text,
  started_at timestamptz,
  finished_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index execution_runs_experiment_id_idx on public.execution_runs (experiment_id);
create index execution_runs_plan_id_idx on public.execution_runs (plan_id);

create trigger execution_runs_set_updated_at
  before update on public.execution_runs
  for each row execute function public.set_updated_at();

create or replace function public.owns_run(r_id uuid)
returns boolean
language sql stable security invoker set search_path = public
as $$
  select exists (
    select 1 from public.execution_runs r
    where r.id = r_id and public.owns_experiment(r.experiment_id)
  )
$$;

-- ---------- execution_run_files: pin the exact code file set per run ----------
-- Stronger than relying on the integer code_version alone: each run references
-- the concrete generated_files rows it executed, so a version can never be
-- silently re-associated with different content.
create table public.execution_run_files (
  run_id uuid not null references public.execution_runs (id) on delete cascade,
  generated_file_id uuid not null references public.generated_files (id) on delete cascade,
  primary key (run_id, generated_file_id)
);

create index execution_run_files_file_idx on public.execution_run_files (generated_file_id);

-- ---------- execution_artifacts ----------
create table public.execution_artifacts (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null references public.execution_runs (id) on delete cascade,
  artifact_type text not null,
  storage_path text not null,
  metadata_json jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index execution_artifacts_run_id_idx on public.execution_artifacts (run_id);

-- ---------- reproduction_reports: one report per run ----------
create table public.reproduction_reports (
  id uuid primary key default gen_random_uuid(),
  run_id uuid not null unique references public.execution_runs (id) on delete cascade,
  report_json jsonb not null,
  markdown text not null,
  created_at timestamptz not null default now()
);

-- ---------- agent_traces: append-only node observability ----------
create table public.agent_traces (
  id uuid primary key default gen_random_uuid(),
  experiment_id uuid not null references public.experiments (id) on delete cascade,
  run_id uuid references public.execution_runs (id),
  node_name text not null,
  input_json jsonb not null default '{}'::jsonb,
  output_json jsonb not null default '{}'::jsonb,
  duration_ms integer,
  created_at timestamptz not null default now()
);

create index agent_traces_experiment_id_idx on public.agent_traces (experiment_id);
create index agent_traces_run_id_idx on public.agent_traces (run_id);

-- ---------- Row Level Security ----------
alter table public.experiments enable row level security;
alter table public.experiment_plans enable row level security;
alter table public.generated_files enable row level security;
alter table public.execution_runs enable row level security;
alter table public.execution_run_files enable row level security;
alter table public.execution_artifacts enable row level security;
alter table public.reproduction_reports enable row level security;
alter table public.agent_traces enable row level security;

create policy experiments_owner_select on public.experiments
  for select using (public.owns_project(project_id));
create policy experiments_owner_insert on public.experiments
  for insert with check (public.owns_project(project_id));
create policy experiments_owner_update on public.experiments
  for update using (public.owns_project(project_id)) with check (public.owns_project(project_id));
create policy experiments_owner_delete on public.experiments
  for delete using (public.owns_project(project_id));

create policy plans_owner_select on public.experiment_plans
  for select using (public.owns_experiment(experiment_id));
create policy plans_owner_insert on public.experiment_plans
  for insert with check (public.owns_experiment(experiment_id));
create policy plans_owner_update on public.experiment_plans
  for update using (public.owns_experiment(experiment_id))
  with check (public.owns_experiment(experiment_id));
create policy plans_owner_delete on public.experiment_plans
  for delete using (public.owns_experiment(experiment_id));

-- Code versions are immutable: select + insert only.
create policy gfiles_owner_select on public.generated_files
  for select using (public.owns_experiment(experiment_id));
create policy gfiles_owner_insert on public.generated_files
  for insert with check (public.owns_experiment(experiment_id));

create policy runs_owner_select on public.execution_runs
  for select using (public.owns_experiment(experiment_id));
create policy runs_owner_insert on public.execution_runs
  for insert with check (public.owns_experiment(experiment_id));
create policy runs_owner_update on public.execution_runs
  for update using (public.owns_experiment(experiment_id))
  with check (public.owns_experiment(experiment_id));
create policy runs_owner_delete on public.execution_runs
  for delete using (public.owns_experiment(experiment_id));

-- Run-to-file pinning is append-only.
create policy runfiles_owner_select on public.execution_run_files
  for select using (public.owns_run(run_id));
create policy runfiles_owner_insert on public.execution_run_files
  for insert with check (public.owns_run(run_id));

create policy artifacts_owner_select on public.execution_artifacts
  for select using (public.owns_run(run_id));
create policy artifacts_owner_insert on public.execution_artifacts
  for insert with check (public.owns_run(run_id));
create policy artifacts_owner_update on public.execution_artifacts
  for update using (public.owns_run(run_id)) with check (public.owns_run(run_id));
create policy artifacts_owner_delete on public.execution_artifacts
  for delete using (public.owns_run(run_id));

create policy reports_owner_select on public.reproduction_reports
  for select using (public.owns_run(run_id));
create policy reports_owner_insert on public.reproduction_reports
  for insert with check (public.owns_run(run_id));
create policy reports_owner_update on public.reproduction_reports
  for update using (public.owns_run(run_id)) with check (public.owns_run(run_id));
create policy reports_owner_delete on public.reproduction_reports
  for delete using (public.owns_run(run_id));

-- Traces are append-only.
create policy traces_owner_select on public.agent_traces
  for select using (public.owns_experiment(experiment_id));
create policy traces_owner_insert on public.agent_traces
  for insert with check (public.owns_experiment(experiment_id));