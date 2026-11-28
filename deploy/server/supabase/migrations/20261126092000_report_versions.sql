-- Task 8: immutable, attempt-pinned report versions.
--
-- Each generation appends a row. The former run_id uniqueness is replaced by
-- (run_id, version), while the persisted report JSON and Markdown are immutable.

alter table public.reproduction_reports
  drop constraint reproduction_reports_run_id_key;
alter table public.reproduction_reports
  add column version integer not null default 1 check (version > 0),
  add column attempt integer,
  add column generation_ms numeric(12,3) check (generation_ms >= 0),
  add column content_sha256 text;

update public.reproduction_reports
set attempt = (
  select max(a.attempt)
  from public.execution_attempts a
  where a.run_id = public.reproduction_reports.run_id
);

-- Hash the exact persisted JSONB representation, never caller-supplied input.
update public.reproduction_reports
set content_sha256 = encode(digest(report_json::text || markdown, 'sha256'), 'hex');
alter table public.reproduction_reports alter column content_sha256 set not null;

create unique index reproduction_reports_run_version
  on public.reproduction_reports (run_id, version);
create unique index reproduction_reports_content_idempotent
  on public.reproduction_reports (run_id, attempt, content_sha256)
  where attempt is not null;
create index reproduction_reports_run_latest
  on public.reproduction_reports (run_id, version desc);

alter table public.reproduction_reports
  add constraint reproduction_reports_run_attempt_fk
  foreign key (run_id, attempt)
  references public.execution_attempts (run_id, attempt)
  deferrable initially deferred;

-- Reports are append-only for application roles. The trigger below also blocks
-- service-role mutation, while retaining foreign-key cleanup cascades.
drop policy if exists reports_owner_update on public.reproduction_reports;
drop policy if exists reports_owner_delete on public.reproduction_reports;
revoke all on public.reproduction_reports from public, anon, authenticated, service_role;
grant select, insert on public.reproduction_reports to anon, authenticated, service_role;
-- TRUNCATE bypasses row-level DELETE triggers and would erase the entire
-- append-only history. No application role receives it.
revoke truncate on public.reproduction_reports from anon, authenticated, service_role;

create or replace function public.report_versions_append_only()
returns trigger
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
begin
  if tg_op = 'DELETE' then
    -- Run/Project cleanup cascades are the only permitted delete path.
    if pg_trigger_depth() <= 1 then
      raise exception 'report history is append-only; delete is allowed only by cascade';
    end if;
    return OLD;
  end if;
  raise exception 'report versions are immutable';
end;
$$;

create trigger reproduction_reports_no_update
before update on public.reproduction_reports
for each row execute function public.report_versions_append_only();
create trigger reproduction_reports_no_delete
before delete on public.reproduction_reports
for each row execute function public.report_versions_append_only();

-- The Run lock serializes version allocation and identical-content replay. The
-- hash is always derived by PostgreSQL from the persisted JSONB representation,
-- never supplied by a caller. NULL-attempt legacy reports are deduplicated under
-- the same lock; the partial unique index additionally protects real attempts.
create or replace function public.append_report_version(
  p_run_id uuid,
  p_report_json jsonb,
  p_markdown text,
  p_generation_ms numeric default null
)
returns table (
  id uuid,
  version integer,
  attempt integer,
  generation_ms numeric
)
language plpgsql
security invoker
set search_path = public, pg_temp
as $$
declare
  v_attempt integer;
  v_hash text;
  v_next_version integer;
begin
  perform 1 from public.execution_runs r where r.id = p_run_id for update;
  if not found then
    raise exception 'REPORT_RUN_NOT_FOUND' using errcode = 'P0001';
  end if;

  select max(a.attempt) into v_attempt
  from public.execution_attempts a where a.run_id = p_run_id;
  v_hash := encode(digest(p_report_json::text || p_markdown, 'sha256'), 'hex');

  return query
  select r.id, r.version, r.attempt, r.generation_ms
  from public.reproduction_reports r
  where r.run_id = p_run_id
    and r.attempt is not distinct from v_attempt
    and r.content_sha256 = v_hash
  order by r.version
  limit 1;
  if found then
    return;
  end if;

  select coalesce(max(r.version), 0) + 1 into v_next_version
  from public.reproduction_reports r where r.run_id = p_run_id;

  insert into public.reproduction_reports (
    run_id, report_json, markdown, version, attempt, generation_ms, content_sha256
  ) values (
    p_run_id, p_report_json, p_markdown, v_next_version, v_attempt,
    p_generation_ms, v_hash
  );

  return query
  select r.id, r.version, r.attempt, r.generation_ms
  from public.reproduction_reports r
  where r.run_id = p_run_id and r.version = v_next_version;
end;
$$;

revoke all on function public.append_report_version(uuid, jsonb, text, numeric)
  from public, anon, authenticated;
-- The invoker RPC locks the mutable Run projection and reads attempt history.
-- Grant exactly those dependencies so service_role can append on a minimally
-- bootstrapped database without receiving any mutation privilege on reports.
grant select, update on public.execution_runs to service_role;
grant select on public.execution_attempts to service_role;
grant execute on function public.append_report_version(uuid, jsonb, text, numeric)
  to service_role;
