-- Workflow cancellation is a terminal outcome, distinct from failure.
alter table public.workflows drop constraint if exists workflows_status_check;
alter table public.workflows add constraint workflows_status_check check (status in (
  'uploaded', 'parsing', 'awaiting_spec_review', 'planning',
  'awaiting_plan_approval', 'generating_code', 'validating_code',
  'executing', 'repairing', 'evaluating', 'completed', 'failed', 'cancelled'
));
