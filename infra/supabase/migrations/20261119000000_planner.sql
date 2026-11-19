-- Experiment Planner (Phase 3): versioned, auditable experiment plans.
-- Config changes (user edit / OOM retry) always INSERT a new version; old
-- versions are never overwritten. A BEFORE UPDATE trigger makes every column
-- except approved_at immutable, so even the service role cannot rewrite a
-- plan's config -- approval stays mutable, config does not.

alter table public.experiment_plans
  add column parent_version_id uuid references public.experiment_plans (id),
  add column origin text not null default 'generated'
    check (origin in ('generated', 'user_edit', 'oom_retry')),
  add column change_reason text,
  add column related_error text,
  add column related_run_id uuid references public.execution_runs (id),
  add column changes_json jsonb not null default '[]'::jsonb;

create index experiment_plans_parent_idx
  on public.experiment_plans (parent_version_id);

-- Only approved_at may be updated; any config/version/provenance change raises.
create or replace function public.experiment_plans_config_immutable()
returns trigger
language plpgsql
as $$
begin
  if old.experiment_id is distinct from new.experiment_id
     or old.version is distinct from new.version
     or old.config_json is distinct from new.config_json
     or old.budget_json is distinct from new.budget_json
     or old.parent_version_id is distinct from new.parent_version_id
     or old.origin is distinct from new.origin
     or old.change_reason is distinct from new.change_reason
     or old.related_error is distinct from new.related_error
     or old.related_run_id is distinct from new.related_run_id
     or old.changes_json is distinct from new.changes_json
     or old.created_at is distinct from new.created_at
  then
    raise exception
      'experiment_plans config is immutable; insert a new version (only approved_at may change)';
  end if;
  return new;
end;
$$;

create trigger experiment_plans_config_immutable
  before update on public.experiment_plans
  for each row execute function public.experiment_plans_config_immutable();

-- Experiments gain an 'approved' state once a plan passes the approval gate.
alter table public.experiments drop constraint if exists experiments_status_check;
alter table public.experiments add constraint experiments_status_check
  check (status in
    ('pending', 'planning', 'approved', 'running', 'succeeded', 'failed', 'cancelled'));