-- Execution domain: audit, error classification and repair tracking.
--
-- The base execution tables (execution_runs, generated_files, execution_run_files,
-- execution_artifacts) already exist with full ownership RLS. This migration adds
-- the columns the Runner/Worker need to record a complete, auditable run:
-- error category/message, repair attempt counter, aggregate code hash, plan
-- version, the Runner-side run id, and the full audit blob (image digest, python
-- version, dependency lock hash, dataset fingerprint, seed, versions).

alter table public.execution_runs
  add column error_category text
    check (error_category in (
      'SYNTAX_ERROR', 'IMPORT_ERROR', 'SHAPE_MISMATCH', 'OUT_OF_MEMORY',
      'TIMEOUT', 'DATA_DOWNLOAD_ERROR', 'METRIC_ERROR', 'UNKNOWN_ERROR'
    )),
  add column error text,
  add column repair_attempt integer not null default 0
    check (repair_attempt between 0 and 3),
  add column code_hash text,
  add column plan_version integer not null default 1 check (plan_version >= 1),
  add column runner_run_id uuid,
  add column audit_json jsonb not null default '{}'::jsonb;

create index execution_runs_runner_run_id_idx
  on public.execution_runs (runner_run_id);