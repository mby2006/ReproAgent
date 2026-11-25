-- Persist the exact Runner request in the same guarded attempt transition that
-- precedes dispatch, so recovery can identify an accepted run after response loss.
alter table public.execution_runs
  add column if not exists request_json jsonb;
