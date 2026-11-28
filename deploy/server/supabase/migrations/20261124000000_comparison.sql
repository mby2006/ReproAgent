-- Paired optimizer comparison domain.
--
-- Ownership root stays projects.user_id: comparison_plans.project_id ->
-- projects, comparisons -> comparison_plans, runs/reports -> comparisons.
-- Plan versions are append-only (approval is the only mutable column), so the
-- exact configuration behind any reported difference can never be rewritten
-- after the fact. Existing single-group experiments/reports are untouched.

-- ---------- comparison_plans ----------
create table public.comparison_plans (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references public.projects (id) on delete cascade,
  paper_id uuid not null references public.papers (id) on delete cascade,
  spec_id uuid not null references public.reproduction_specs (id),
  name text not null check (char_length(name) between 1 and 200),
  status text not null default 'draft'
    check (status in ('draft', 'awaiting_confirmation', 'approved', 'superseded')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index comparison_plans_project_idx on public.comparison_plans (project_id);
create index comparison_plans_paper_idx on public.comparison_plans (paper_id);

create trigger comparison_plans_set_updated_at
  before update on public.comparison_plans
  for each row execute function public.set_updated_at();

create or replace function public.owns_comparison_plan(cp_id uuid)
returns boolean
language sql stable security invoker set search_path = public
as $$
  select exists (
    select 1 from public.comparison_plans cp
    where cp.id = cp_id and public.owns_project(cp.project_id)
  )
$$;

-- ---------- comparison_plan_versions: append-only, like reproduction_specs ----------
create table public.comparison_plan_versions (
  id uuid primary key default gen_random_uuid(),
  comparison_plan_id uuid not null references public.comparison_plans (id) on delete cascade,
  version integer not null check (version >= 1),
  plan_json jsonb not null,
  origin text not null default 'generated'
    check (origin in ('generated', 'user_edit', 'scaling_change', 'rerun')),
  parent_version_id uuid references public.comparison_plan_versions (id),
  change_reason text,
  approved_at timestamptz,
  created_at timestamptz not null default now(),
  unique (comparison_plan_id, version)
);

create index comparison_plan_versions_plan_idx
  on public.comparison_plan_versions (comparison_plan_id, version desc);

create or replace function public.comparison_plan_versions_immutable()
returns trigger
language plpgsql
as $$
begin
  if old.comparison_plan_id is distinct from new.comparison_plan_id
     or old.version is distinct from new.version
     or old.plan_json is distinct from new.plan_json
     or old.origin is distinct from new.origin
     or old.parent_version_id is distinct from new.parent_version_id
     or old.change_reason is distinct from new.change_reason
     or old.created_at is distinct from new.created_at
  then
    raise exception
      'comparison_plan_versions is immutable; insert a new version (only approved_at may change)';
  end if;
  return new;
end;
$$;

create trigger comparison_plan_versions_immutable
  before update on public.comparison_plan_versions
  for each row execute function public.comparison_plan_versions_immutable();

-- ---------- comparisons ----------
create table public.comparisons (
  id uuid primary key default gen_random_uuid(),
  project_id uuid not null references public.projects (id) on delete cascade,
  plan_id uuid not null references public.comparison_plans (id) on delete cascade,
  plan_version_id uuid not null references public.comparison_plan_versions (id),
  status text not null default 'awaiting_confirmation'
    check (status in (
      'draft', 'awaiting_confirmation', 'awaiting_approval',
      'queued', 'running', 'completed', 'incomplete', 'failed'
    )),
  origin text not null default 'new'
    check (origin in ('new', 'rerun', 'rerun_current_env')),
  source_comparison_id uuid references public.comparisons (id),
  idempotency_key text,
  requested_pairs integer not null default 1 check (requested_pairs >= 1),
  completed_pairs integer not null default 0 check (completed_pairs >= 0),
  question text,
  summary_json jsonb not null default '{}'::jsonb,
  error text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- Repeated identical start requests must enqueue at most once per plan.
create unique index comparisons_idempotent_start
  on public.comparisons (plan_id, idempotency_key)
  where idempotency_key is not null;

create index comparisons_project_idx on public.comparisons (project_id);
create index comparisons_plan_idx on public.comparisons (plan_id);
create index comparisons_status_idx on public.comparisons (status);

create trigger comparisons_set_updated_at
  before update on public.comparisons
  for each row execute function public.set_updated_at();

create or replace function public.owns_comparison(c_id uuid)
returns boolean
language sql stable security invoker set search_path = public
as $$
  select exists (
    select 1 from public.comparisons c
    where c.id = c_id and public.owns_project(c.project_id)
  )
$$;

-- ---------- comparison_runs: one row per (comparison, seed, group, attempt) ----------
create table public.comparison_runs (
  id uuid primary key default gen_random_uuid(),
  comparison_id uuid not null references public.comparisons (id) on delete cascade,
  seed integer not null,
  group_key text not null check (group_key in ('a', 'b')),
  run_id uuid not null references public.execution_runs (id) on delete cascade,
  attempt integer not null default 1 check (attempt >= 1),
  pair_verified boolean,
  created_at timestamptz not null default now(),
  unique (comparison_id, seed, group_key, attempt)
);

create index comparison_runs_comparison_idx on public.comparison_runs (comparison_id);
create index comparison_runs_run_idx on public.comparison_runs (run_id);

-- ---------- comparison_reports ----------
create table public.comparison_reports (
  id uuid primary key default gen_random_uuid(),
  comparison_id uuid not null unique references public.comparisons (id) on delete cascade,
  report_json jsonb not null,
  markdown text not null,
  status text not null check (status in ('complete', 'incomplete')),
  created_at timestamptz not null default now()
);

create index comparison_reports_comparison_idx on public.comparison_reports (comparison_id);

-- ---------- Row Level Security ----------
alter table public.comparison_plans enable row level security;
alter table public.comparison_plan_versions enable row level security;
alter table public.comparisons enable row level security;
alter table public.comparison_runs enable row level security;
alter table public.comparison_reports enable row level security;

create policy comparison_plans_owner_select on public.comparison_plans
  for select using (public.owns_project(project_id));
create policy comparison_plans_owner_insert on public.comparison_plans
  for insert with check (public.owns_project(project_id));
create policy comparison_plans_owner_update on public.comparison_plans
  for update using (public.owns_project(project_id))
  with check (public.owns_project(project_id));

-- Plan versions are append-only for clients; approval is a service-role write
-- guarded by the immutability trigger above.
create policy comparison_plan_versions_owner_select on public.comparison_plan_versions
  for select using (public.owns_comparison_plan(comparison_plan_id));
create policy comparison_plan_versions_owner_insert on public.comparison_plan_versions
  for insert with check (public.owns_comparison_plan(comparison_plan_id));

create policy comparisons_owner_select on public.comparisons
  for select using (public.owns_project(project_id));
create policy comparisons_owner_insert on public.comparisons
  for insert with check (public.owns_project(project_id));
create policy comparisons_owner_update on public.comparisons
  for update using (public.owns_project(project_id))
  with check (public.owns_project(project_id));

create policy comparison_runs_owner_select on public.comparison_runs
  for select using (public.owns_comparison(comparison_id));
create policy comparison_runs_owner_insert on public.comparison_runs
  for insert with check (public.owns_comparison(comparison_id));

create policy comparison_reports_owner_select on public.comparison_reports
  for select using (public.owns_comparison(comparison_id));
create policy comparison_reports_owner_insert on public.comparison_reports
  for insert with check (public.owns_comparison(comparison_id));
create policy comparison_reports_owner_update on public.comparison_reports
  for update using (public.owns_comparison(comparison_id))
  with check (public.owns_comparison(comparison_id));
