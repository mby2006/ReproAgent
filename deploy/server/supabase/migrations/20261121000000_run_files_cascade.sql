-- execution_run_files pins a run to concrete generated_files rows. Both parents
-- (execution_runs, generated_files) cascade from experiments, but Postgres does
-- not guarantee deletion order: generated_files could be removed before the pin
-- rows, raising a FK violation during experiment/user teardown. Cascade the
-- generated_file_id FK too so teardown is order-independent.

alter table public.execution_run_files
  drop constraint execution_run_files_generated_file_id_fkey;

alter table public.execution_run_files
  add constraint execution_run_files_generated_file_id_fkey
  foreign key (generated_file_id)
  references public.generated_files (id) on delete cascade;