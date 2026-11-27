# Task 9 Report — Recovery, Security, and Ownership Negative Matrix

## Status

DONE. Task 9 implements repeatable G-03/G-04 negative coverage against a disposable real PostgreSQL database, real Runner sibling containers, and the production API ownership boundary.

BASE: `8eb46bfa11104f6afffd869a4c71a266cff0e663`

## What Changed

- Added `tests/integration/test_negative_matrix.py` with a narrow PostgREST-shaped adapter that executes workflow operations as real SQL.
- Added terminal/recovery coverage for unrecoverable failure, hard timeout, and user cancellation.
- Added cancellation injection after Runner acceptance and before result persistence.
- Added fourth-ordinary-repair refusal, immutable attempt/file projections, code-version parent history, and final-attempt report assertions.
- Added third-OOM-replan refusal, batch-only reduction, parent Plan links, unchanged critical assumptions, attempt increments, and final-attempt report references.
- Added RLS and service-role API/RPC ownership denial matrices, including guessed UUIDs for logs, artifacts, reports, traces, and evidence-related routes.
- Added `apps/runner/tests/test_sandbox.py` using real Docker container attributes and in-container observations for network, capabilities, privileges, root filesystem, user, binds, credentials, Docker socket, and resource limits.
- Added disposable database setup in `tests/integration/conftest.py`; it clones a configured local template, applies all migrations, rolls back each test, and force-drops the database.
- Added explicit stock-Postgres service-role grants and storage RLS enablement in `20261126103000_service_role_authorization.sql` without changing ownership policies.
- Preserved hard timeout as non-repairable terminal truth in `codegen/classify.py`.
- Reconciled in-memory Runner results against protected persisted terminal Run state in `workflow/nodes.py`, preventing a concurrent cancel/terminal seal from being replaced by Runner output.
- Added the lifecycle cancellation regression and updated the integration gate while retaining the workflow-lock test.
- Updated workflow E2E wording and assertions to state the fixture-LLM limitation and verify attempt/artifact/report/ownership truth.
- Updated the release migration-count assertion from 14 to 15 for the new migration.

## TDD Evidence

### RED

- `uv run pytest tests/integration/test_negative_matrix.py apps/runner/tests/test_sandbox.py -q --disable-warnings`
- Initial real-Docker assertion failed because Windows bind sources contain a drive-letter colon. The old `bind.split(":")[1:]` retained host-path components and did not equal the expected container target/mode pairs.
- Full offline verification after adding the migration exposed:
  `tests/unit/test_release_ops.py::test_repository_migrations_have_no_transaction_control`
  expected 14 migrations but found 15.

### GREEN

- Windows bind parsing now uses trailing target/mode fields and separately verifies the exact host source directories.
- Release migration-count assertion now expects 15 while retaining every transaction-control assertion.

## Verification Evidence

All commands were run from `C:/Users/demon/Desktop/ReproAgent`.

| Command | Result |
|---|---|
| `uv run pytest -o addopts="--import-mode=importlib -m 'not integration'" -q --disable-warnings` | **1258 passed, 151 deselected**, 34 warnings, 122.47s |
| `REPRO_TEST_DATABASE_TEMPLATE=reproagent_task8_template uv run pytest tests/integration/test_negative_matrix.py -m integration -q --disable-warnings` | **10 passed** |
| `uv run pytest apps/runner/tests/test_sandbox.py -q --disable-warnings` | **2 passed** against real Docker |
| `uv run pytest apps/api/tests/test_run_lifecycle.py -q --disable-warnings` | **48 passed** |
| `uv run pytest apps/api/tests/test_workflow_cancellation.py apps/api/tests/test_reports.py -q --disable-warnings` | **36 passed** |
| `REPRO_TEST_DATABASE_TEMPLATE=reproagent_task8_template uv run pytest tests/integration/test_workflow_lock.py tests/integration/test_run_authorization.py tests/integration/test_attempt_history.py -m integration -q --disable-warnings` | **89 passed** |
| `REPRO_TEST_DATABASE_TEMPLATE=reproagent_task8_template uv run pytest tests/integration/test_workflow_e2e.py -m integration -q --disable-warnings` | **2 passed** with real PostgreSQL/Docker and fixture LLM |
| `uv run ruff check` on all Task 9 Python files | **All checks passed** |
| `git diff --check` | Clean; only normal Git LF/CRLF informational warnings |

Focused reruns of the new Docker/lifecycle suites and new PostgreSQL matrix/authorization/attempt suites emitted no warnings. The 34 warnings belong to the broader offline suite and are not emitted by the focused new coverage.

A repository-wide `uv run ruff check .` was also attempted. It reports 154 existing issues in unrelated `.agents/` content and legacy tests; those files were intentionally not modified. Task 9 touched-file lint is clean.

## Files Changed

- `apps/agent/src/reproagent_agent/codegen/classify.py`
- `apps/api/src/reproagent_api/workflow/nodes.py`
- `apps/api/tests/test_run_lifecycle.py`
- `apps/runner/tests/test_sandbox.py`
- `infra/supabase/migrations/20261126103000_service_role_authorization.sql`
- `scripts/check_integration.sh`
- `tests/integration/conftest.py`
- `tests/integration/test_negative_matrix.py`
- `tests/integration/test_run_authorization.py`
- `tests/integration/test_workflow_e2e.py`
- `tests/unit/test_release_ops.py`

## Self-Review

- Confirmed sandbox expectations were not weakened after the Windows parsing fix; host source and container target/mode are both asserted.
- Confirmed integration tests use disposable databases only.
- Confirmed no generated program executes in the pytest/API process; Runner execution remains sibling-container-only.
- Confirmed timeout, cancellation, attempt, workflow, and report truths are asserted separately.
- Confirmed service-role visibility does not substitute for router ownership checks.
- Confirmed unrelated untracked directories and pre-existing local files remain untouched.
- Fixed the only full-suite regression exposed by Task 9: the migration count assertion.

## Concerns / Evidence Limits

- Workflow E2E remains fixture-backed at the LLM boundary. PostgreSQL, workflow logic, and Docker training are real, but this is **not** evidence of a real Qwen closed loop.
- The broader suite's 34 warnings and repository-wide unrelated Ruff debt remain visible and are not represented as Task 9 regressions.
- Three implementation/finisher subagents were interrupted by turn/runtime failures; the controller preserved their work and completed the final mechanical verification, report, and commit sequentially. Independent task review is still required before Task 9 is marked complete.

## Fix round 1

### Root causes

1. **Terminal persistence race:** `async_persist_result` claimed a nonterminal Run, performed external I/O, deleted current-attempt artifact rows, and only then updated the Run while excluding `cancelled` and `succeeded`. A concurrent `failed`, `timeout`, or `succeeded` seal could therefore receive late audit fields, and cancellation could erase current-attempt evidence.
2. **Invalid security assertion target:** Task 9 asserted foreign-owner and guessed-UUID denial on `/v1/runs/{run_id}/evidence-index`, but that route does not exist yet. A 404/403 from the absent route could not prove authorization behavior.
3. **Superuser-masked ACL gaps:** Positive workflow/authorization tests executed as the PostgreSQL superuser. Stock `service_role` lacked privileges required by real owner-positive paths, so the tests did not exercise the production database role.

### Changes

- Added `infra/supabase/migrations/20261126104000_atomic_result_persistence.sql` with `public.persist_execution_result`. The function locks the Run row, rechecks the matching 0-based `repair_attempt` and nonterminal state, requires the matching 1-based attempt-history row, replaces only current-attempt artifacts, and updates audit fields in one transaction. Any artifact insert failure rolls back both deletion and audit update.
- Moved all destructive result-persistence database work into that RPC. Storage and Runner artifact I/O still happen first, but no artifact rows are deleted or inserted before the lock/guard.
- Preserved append-only attempt/file/report history. The RPC never mutates attempt or report history, and prior-attempt artifacts are outside its deletion predicate.
- Added offline and real-PostgreSQL regressions that inject after the result claim and before artifact replacement for `cancelled`, `failed`, `timeout`, and `succeeded`. They assert protected artifact identity plus terminal status, error/category, metrics, audit, log paths, finished time, attempt seal, and recovery stability.
- Added a real-SQL rollback control proving a malformed artifact cannot partially erase current evidence or update Run audit fields.
- Added actual-`service_role` owner-positive controls for Plan edit/approval, Run creation and idempotent replay, outbox/idempotency writes, cancellation, report append, generated files, attempts/files/projection, artifact persistence, traces, workflow updates, and OOM Plan insertion. Controls assert `current_user='service_role'`, `rolsuper=false`, and `rolbypassrls=true`.
- Switched authorization integration execution to `service_role`; only auth-user scaffolding temporarily uses the privileged role. Positive and negative RPC decisions now run under the production role.
- Added focused service-role pinning/projection/report coverage and an exact table-privilege matrix. The migration grants only required production privileges, removes broad outbox/idempotency grants, and does not grant DELETE on append-only history. Card versions and artifact rows receive no UPDATE grant.
- Removed only the two `/evidence-index` assertions from Task 9. Foreign-owner and guessed-UUID denial coverage for existing logs, artifacts, report, files, prediction-examples, and traces routes remains intact.
- Updated the offline PostgREST stub and real-SQL adapter for the new RPC, including UUID/integer/numeric/JSONB casts and scalar UUID results.
- Updated the release migration-count assertion from 15 to 16.

**Task 12 delegation:** actual foreign-owner and guessed-UUID denial verification for `GET /v1/runs/{run_id}/evidence-index` is delegated to Task 12, which owns that endpoint. Task 9 does not implement the route early and does not claim evidence-index authorization evidence.

### Covering tests

- `apps/api/tests/test_run_lifecycle.py`: post-claim cancellation and FAILED/TIMEOUT/SUCCEEDED terminal-audit preservation.
- `apps/api/tests/test_runs.py`: offline RPC guard, attempt matching, current-attempt replacement, and audit update behavior.
- `tests/integration/test_negative_matrix.py`: real RPC race matrix, rollback atomicity, exact service-role ACLs, owner-positive workflow/execution/OOM controls, and retained ownership denials.
- `tests/integration/test_run_authorization.py`: authorization and idempotency decisions as actual `service_role`.
- `tests/integration/test_attempt_history.py`: service-role pin/projection/report positive control plus existing append-only and concurrency coverage.
- `tests/integration/test_workflow_e2e.py`: retained real Supabase/Docker closed loop and existing-route ownership/guessed-UUID denials.

### TDD evidence

- The four new offline post-claim terminal cases initially failed because the protected artifact was replaced by `late.json`; the atomic RPC made the focused lifecycle selection pass (`6 passed`).
- The first real-`service_role` positive control failed with `psycopg.errors.InsufficientPrivilege: permission denied for table projects` and the hint `GRANT INSERT ON public.projects TO service_role`, proving superuser execution had masked the production ACL gap.
- Real RPC execution initially exposed adapter-only UUID/JSONB/numeric cast gaps; fixing the adapter allowed all four real terminal-preservation cases to pass without changing production expectations.
- The first required integration run reached the real-Supabase E2E but its local test database lacked the newly added migration, so the RPC fell back to an empty metric result and `run["metrics_json"]["target_metric"]` raised `KeyError`. After applying the new migration to the local Supabase E2E database, the complete required integration command passed.

### Verification evidence

All commands were run from `C:/Users/demon/Desktop/ReproAgent`.

| Command | Output / result |
|---|---|
| `REPRO_TEST_DATABASE_TEMPLATE=reproagent_task8_template uv run pytest tests/integration/test_negative_matrix.py tests/integration/test_run_authorization.py tests/integration/test_attempt_history.py tests/integration/test_workflow_e2e.py -m integration -q --disable-warnings` | Passed; 112 collected tests, progress reached `[100%]` with real PostgreSQL, local Supabase, and Docker. |
| `uv run pytest apps/api/tests/test_runs.py apps/api/tests/test_run_lifecycle.py apps/api/tests/test_workflow_cancellation.py apps/api/tests/test_reports.py -q --disable-warnings` | Passed; 172 collected tests, progress reached `[100%]`. |
| `uv run pytest -q` | Passed; 1262 offline tests, 160 integration tests deselected. Existing Starlette deprecation warnings remain visible. |
| `uv run ruff check apps/api/src/reproagent_api/services/runs.py apps/api/tests/test_run_lifecycle.py apps/api/tests/test_runs.py tests/integration/test_attempt_history.py tests/integration/test_negative_matrix.py tests/integration/test_run_authorization.py tests/integration/test_workflow_e2e.py tests/unit/test_release_ops.py` | `All checks passed!` |
| `uv run ruff format --check` on the same eight Python files | Reports `8 files would be reformatted`; whole-file formatting was not applied because it would rewrite unrelated existing code. |
| `bash -n scripts/check_integration.sh` | Exit 0; no output. |
| `git diff --check` | Clean; only Git LF/CRLF informational warnings. |

### Evidence limits

- Workflow E2E still uses a fixture LLM boundary. PostgreSQL, workflow logic, authorization SQL, and Docker training are real, but this is not evidence of a real Qwen closed loop.
- Evidence-index authorization remains unverified until Task 12 implements and tests the route.
- Whole-file Ruff formatting debt remains in the touched files; touched-file Ruff lint is clean.
- No subagents were dispatched, per the fix-round scope instruction; independent subagent review was therefore not performed.

## Fix round 2

### Verified constraint and scope adjudication

- Independent review verified that `storage.objects` and `storage.buckets` are platform-managed by Supabase. On the bootstrapped database their grantor/ownership relationship belongs to `supabase_storage_admin`; the migration runs as non-superuser `postgres`, which cannot assume that role or revoke its grants. The former exact Storage matrix therefore passed only because the cloned test template did not reproduce the production relationship, while RLS cannot constrain `service_role` because it has `BYPASSRLS`.
- Controller adjudication keeps Task 9 scoped to public application tables. The API intentionally uses the high-privilege service-role key, and Storage upsert legitimately requires UPDATE on `storage.objects`. Platform Storage least-privilege redesign is outside this task.

### Changes

- Removed only the final Storage `REVOKE`/`GRANT` statements from `20261126104000_atomic_result_persistence.sql`, replacing them with a note that Supabase manages Storage ACLs and the real-Supabase E2E validates required privileges. Public application grants/revokes and `persist_execution_result` are unchanged.
- Removed `storage.objects` and `storage.buckets` from the exact expected map in `test_service_role_grants_are_scoped_to_production_writes`. The exact seven-privilege matrix remains for every public application table; append-only and RLS tests are unchanged.
- Added `TestFullClosedLoop.test_service_role_has_required_storage_privileges` in `test_workflow_e2e.py`. It queries `has_table_privilege` against real Supabase and requires SELECT/INSERT/UPDATE/DELETE on `storage.objects` and SELECT/INSERT on `storage.buckets`. It explicitly permits additional platform-managed privileges and does not claim exact Storage ACLs. The existing closed loop exercises the actual service-role upload/upsert/download path.

### Verification evidence

All commands were run from `C:/Users/demon/Desktop/ReproAgent`.

| Command | Result |
|---|---|
| `REPRO_TEST_DATABASE_TEMPLATE=reproagent_task8_template uv run pytest tests/integration/test_negative_matrix.py -m integration -q --disable-warnings` | **22 passed** |
| `uv run pytest tests/integration/test_workflow_e2e.py -m integration -q --disable-warnings` | **3 passed** against real Supabase/PostgreSQL and Docker |
| `uv run ruff check tests/integration/test_negative_matrix.py tests/integration/test_workflow_e2e.py` | `All checks passed!` |
| `git diff --check` | Clean; only normal Git LF/CRLF informational warnings |

The first negative-matrix rerun failed because `reproagent_task8_template` had no service-role Storage grants: inspection showed both Storage tables were owned by `postgres`, not `supabase_storage_admin`, with no ACL entries. This confirmed the template does not reproduce production Storage ownership. Bootstrapping the template with the same required Storage privileges Supabase provides, without changing repository files, allowed the unchanged matrix to pass. Storage is platform-managed; the real-Supabase E2E proves the required privileges and actual upload/upsert path, not exact Storage ACLs.

## Completion

Final scoped independent re-review: **APPROVED**, no Important/Critical defects. Task 9 is complete through commit `d9ab60e`. The evidence-index endpoint's foreign-owner and guessed-UUID denial tests remain an explicit Task 12 acceptance dependency; Task 9 does not claim that evidence.
