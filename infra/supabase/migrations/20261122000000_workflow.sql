-- Unified LangGraph reproduction workflow (Phase D).
-- A workflow is the single checkpointed thread tying paper -> spec -> plan ->
-- code -> run -> report, with a 12-state status machine. Ownership flows
-- workflows.project_id -> projects.user_id. agent_traces becomes workflow-scoped
-- (experiment_id nullable) and gains model/token/error/retry observability.

-- ---------- workflows ----------
create table public.workflows (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references public.projects (id) on delete cascade,
  paper_id uuid not null references public.papers (id) on delete cascade,
  thread_id text not null unique,
  status text not null default 'uploaded'
    check (status in (
      'uploaded', 'parsing', 'awaiting_spec_review', 'planning',
      'awaiting_plan_approval', 'generating_code', 'validating_code',
      'executing', 'repairing', 'evaluating', 'completed', 'failed'
    )),
  current_node text,
  experiment_id uuid references public.experiments (id) on delete set null,
  run_id uuid references public.execution_runs (id) on delete set null,
  spec_version_id uuid references public.reproduction_specs (id) on delete set null,
  plan_id uuid references public.experiment_plans (id) on delete set null,
  budget_json jsonb not null default '{}'::jsonb,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index workflows_project_id_idx on public.workflows (project_id);
create index workflows_paper_id_idx on public.workflows (paper_id);
create index workflows_status_idx on public.workflows (status);

create trigger workflows_set_updated_at
  before update on public.workflows
  for each row execute function public.set_updated_at();

create or replace function public.owns_workflow(w_id uuid)
returns boolean
language sql stable security invoker set search_path = public
as $$
  select exists (
    select 1 from public.workflows w
    where w.id = w_id and public.owns_project(w.project_id)
  )
$$;

-- ---------- agent_traces: workflow-scoped node observability ----------
alter table public.agent_traces alter column experiment_id drop not null;
alter table public.agent_traces
  add column workflow_id uuid references public.workflows (id) on delete cascade,
  add column model_name text,
  add column token_usage jsonb,
  add column error text,
  add column retry_count integer not null default 0;

create index agent_traces_workflow_id_idx on public.agent_traces (workflow_id);

-- ---------- Row Level Security ----------
alter table public.workflows enable row level security;

create policy workflows_owner_select on public.workflows
  for select using (public.owns_project(project_id));
create policy workflows_owner_insert on public.workflows
  for insert with check (public.owns_project(project_id));
create policy workflows_owner_update on public.workflows
  for update using (public.owns_project(project_id)) with check (public.owns_project(project_id));
create policy workflows_owner_delete on public.workflows
  for delete using (public.owns_project(project_id));

-- Traces remain append-only. Ownership now flows via workflow (understanding
-- nodes precede experiment creation) or, for legacy rows, via experiment.
drop policy if exists traces_owner_select on public.agent_traces;
drop policy if exists traces_owner_insert on public.agent_traces;

create policy traces_owner_select on public.agent_traces
  for select using (
    (workflow_id is not null and public.owns_workflow(workflow_id))
    or (experiment_id is not null and public.owns_experiment(experiment_id))
  );
create policy traces_owner_insert on public.agent_traces
  for insert with check (
    (workflow_id is not null and public.owns_workflow(workflow_id))
    or (experiment_id is not null and public.owns_experiment(experiment_id))
  );