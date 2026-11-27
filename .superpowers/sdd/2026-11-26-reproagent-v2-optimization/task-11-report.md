# Task 11 Report: 首屏论文条件、批准计划与实际实现对照

## Status

DONE_WITH_CONCERNS

- Branch: `chore/p0-p1-p2-hardening`
- Base: `ccd1bc94af3cfa2888910d8ff6eb4a611dcae1e7`
- Date: 2026-11-26

## What changed

Implemented an immediately visible, read-only condition summary on both requested surfaces:

1. Authenticated experiment workbench (`PlanClient.tsx`)
   - Uses evaluator `report_json.parameter_comparisons` when the report belongs to the currently viewed immutable plan version.
   - Falls back to approved-plan rows before any report exists and explicitly displays `尚未执行` for actual implementation.
   - If a report exists but does not match the viewed plan version, actual displays `无可用证据`; it is never inferred from the plan.
   - Shows report version and final attempt in the summary header when available.
   - Keeps the existing full five-column `ParameterTable` behind a disclosure for decision, evidence, and impact details.

2. Public LeNet archive (`examples/lenet-mnist/page.tsx`)
   - Reads the existing `public/examples/lenet/conditions.json`.
   - Calls the same pure selector as the workbench.
   - Places the summary immediately after the masthead and before navigation/evidence disclosure.
   - Preserves paper page links, actual evidence kind/field, and archive file links.

3. Shared selector and renderer
   - Added `selectConditionSummary(rows: Array<Record<string, unknown>>) -> Array<Record<string, unknown>>`.
   - Fixed order: `dataset`, `model architecture`, `optimizer`, `learning rate`, `loss function`, `batch size`, `epochs`, `target metric`.
   - Maps evaluator keys such as `architecture`, `learning_rate`, `loss_function`, `batch_size`, and `target_metric`, plus public/legacy aliases.
   - Missing values are explicit as `无可用证据`.
   - Preserves `paper_evidence`, `actual_evidence`, `source`, `note`, and `decision`.
   - Added a responsive `ConditionSummary` renderer beside the unchanged full `ParameterTable`.

## Files changed

- `apps/web/src/lib/conditions.mjs` (new)
- `apps/web/src/lib/conditions.test.mjs` (new)
- `apps/web/src/lib/types.ts`
- `apps/web/src/components/ParameterTable.tsx`
- `apps/web/src/app/experiments/PlanClient.tsx`
- `apps/web/src/app/examples/lenet-mnist/page.tsx`
- `apps/web/src/app/examples/lenet-mnist/page.test.mjs`
- `.superpowers/sdd/2026-11-26-reproagent-v2-optimization/task-11-report.md` (this report)

## TDD evidence

### RED

Command:

```bash
cd apps/web && node --test src/lib/conditions.test.mjs
```

Initial result failed because `conditions.mjs` did not exist (`ERR_MODULE_NOT_FOUND`), confirming the test was red before implementation. Expanded mapping assertions then failed on missing canonical `key` and source-section fallback before those behaviors were implemented.

The public page regression also failed before integration with `condition summary is visible`, confirming the summary was not present on the first screen.

### GREEN

Command:

```bash
cd apps/web && node --test src/lib/conditions.test.mjs
```

Result:

```text
✔ condition summary keeps paper plan actual order and never invents missing values
✔ condition summary maps evaluator and public keys while preserving table evidence
ℹ tests 2
ℹ pass 2
ℹ fail 0
```

## Verification

### Automated Web checks

All commands were run from `apps/web` after the final source edits:

```text
pnpm test
  67 tests, 67 passed, 0 failed

pnpm lint
  passed

pnpm exec tsc --noEmit
  passed

pnpm build
  Next.js 16.4.6 production live build passed
  Built and verified live artifact
```

### Static archive checks

```text
pnpm build:static
  Archive run 01993dd36ff17102: verifiedFiles: 9
  Built and verified static artifact

pnpm verify:static
  Verified static artifact: C:\Users\demon\Desktop\ReproAgent\apps\web\out
  Archive run 01993dd36ff17102: verifiedFiles: 9
```

The public archive remains exactly nine manifest-verified files.

### Real browser verification

Used installed Microsoft Edge through `playwright-core`. Restarted `reproagent-web-acceptance` after the final live build before browser testing.

Public archive, final build:

- `1440px`: paper/plan/actual cells are side-by-side on the same Y coordinate.
- `390px`: cells stack in paper → plan → actual order.
- No page-level horizontal overflow at either viewport.
- A 1200-character unbroken value wraps without cell or page overflow.
- Full five-column table is hidden while collapsed and visible after expansion.
- Summary appears before the evidence disclosure.

Evidence screenshots:

- `.superpowers/sdd/2026-11-26-reproagent-v2-optimization/task-11-public-1440.png`
- `.superpowers/sdd/2026-11-26-reproagent-v2-optimization/task-11-public-390.png`

Authenticated workbench:

- Verified at `1440px` and `390px` with a dedicated local Supabase test account.
- Confirmed `报告 v3` and `最终尝试 2`.
- Confirmed evaluator actual is used rather than plan: plan `SGD` and actual `adam` both render.
- Confirmed actual metric `11.23%`.
- Confirmed no-report state renders `尚未执行` and omits the report-version chip.
- Confirmed long-text wrapping and collapsed full-table behavior.
- Ephemeral account was deleted; remaining Task 11 ephemeral users: 0.

Evidence screenshots:

- `.superpowers/sdd/2026-11-26-reproagent-v2-optimization/task-11-workbench-1440.png`
- `.superpowers/sdd/2026-11-26-reproagent-v2-optimization/task-11-workbench-390.png`

## Self-review

- The selector is pure and shared by both surfaces; it does not compute or infer values.
- Workbench report actuals are bound to `report_json.references.plan_version === current.version`, preserving immutable report identity.
- Missing actuals are explicit: `尚未执行` before report generation and `无可用证据` when a report cannot be tied to the viewed plan.
- Existing full five-column `ParameterTable` behavior remains available for details.
- Paper page/source, notes, actual evidence kind/field, and file behavior are preserved.
- Desktop and mobile layouts passed real Edge checks, including long-text overflow and collapsed-table behavior.
- `git diff --check` passed; only line-ending warnings were emitted.
- No secrets, credentials, tokens, temporary browser scripts, or unrelated untracked files are intended for the commit.
- Existing unrelated untracked paths remain untouched: `apps/web/report-v2.md`, `docs/superpowers/`, and `report-v2.md`.

## Concerns

- Workbench browser verification used deterministic route fixtures to prove layout and data-flow behavior; it was not a newly executed scientific training run. The public archive used real manifest data, and evaluator key names/evidence fields were checked against `apps/agent/src/reproagent_agent/evaluation/parameters.py`. No fixture data is committed.

## Fix round 1/5

- Session model: inherited because model selection is unavailable in this harness.
- Base: `e74e5d0e9fec43239e8569fd938005b67d22ea28`
- Fix commit: `437f4f559b587707716bf3d252d8de565c87b2cc`

### Root causes

1. Plan-derived condition rows copied `PlanParam.paper_value`, `evidence_quote`, and `source_page` without checking `PlanParam.source`. The planner can attach candidate evidence to `INFERRED` params, so the summary promoted unverified candidates into the paper column. `EvaluationConfig` also lacks an evidence-status flag, making `reported_value` plus `reported_source_page` insufficient proof of paper provenance.
2. The report effect collapsed every non-`REPORT_UNAVAILABLE` failure into the same empty state as “no report”. Row rendering then used `report ? 无可用证据 : 尚未执行`, so network and report-generation failures falsely asserted that execution had not happened.
3. The effect inspected only `runs[0]`. Because runs are newest-first, a newer unavailable/failed/wrong-version report hid an older persisted report that still matched the current immutable plan version.

### Changes

- Added provenance gates for pre-report paper values and citations: only `PlanParam.source === "paper_explicit"` can populate the paper column or paper evidence.
- Suppressed the plan's target-metric reported result as paper evidence before an evaluator report because `EvaluationConfig` has no trustworthy verification flag. The separate evaluation card now labels it `提取报告值（论文出处未验证）` instead of `原文报告`.
- Added an explicit `ConditionReportState` discriminated state: `loading`, `ready`, `unavailable`, and `unknown`.
  - `loading` renders `正在确认`.
  - Confirmed no-report states render `尚未执行`.
  - Network/service/report-generation failures and unusable version matches render `无法确认`.
  - Ready reports without comparisons render `无可用证据`.
- Added `loadConditionReport`, which walks runs newest-to-oldest, continues through `REPORT_UNAVAILABLE` and other report errors, and accepts only a report whose `report_json.references.plan_version` matches the current plan version.
- Added a render-time plan-version guard so a stale ready report cannot flash comparisons after switching plan versions.
- Preserved the fixed eight-item order, `无可用证据` missing-value copy, five-column detailed table, responsive three-column summary, and report version/final-attempt metadata behavior.

### TDD evidence

#### RED

First focused run after adding `PlanClient.test.mjs` failed all 10 new tests because the condition-row and report-selection seams were not exported. After adding the test seams without correcting behavior, the focused suite produced 7 behavioral failures, including:

- inferred optimizer `adam` rendered as paper evidence instead of `无可用证据`;
- unverified target result `99.1（test）` rendered as paper evidence;
- newest `REPORT_UNAVAILABLE` and `REPORT_GENERATION_FAILED` did not fall through to the older matching report;
- generation/network failures became `unavailable` and would render `尚未执行`;
- a report for another plan version was not rejected correctly.

A further RED test proved that a stale `ready` report for plan v1 could still render after switching to plan v2.

#### GREEN

```text
cd apps/web && node --test src/app/experiments/PlanClient.test.mjs src/lib/conditions.test.mjs
tests 13
pass 13
fail 0
```

The new tests cover inferred paper candidates, explicit paper provenance, unverified target metrics, unavailable and generation-failed newest reports, network failures, older matching reports, wrong-plan-version reports, stale version switches, confirmed no-report states, loading, and ready reports without comparisons.

### Verification

All commands were run after the final source edits:

```text
cd apps/web && pnpm test
  78 tests, 78 passed, 0 failed

cd apps/web && pnpm lint
  passed

cd apps/web && pnpm exec tsc --noEmit
  passed

cd apps/web && pnpm build
  Next.js 16.4.6 live production build passed
  Built and verified live artifact

cd apps/web && pnpm build:static
  Archive run 01993dd36ff17102: verifiedFiles: 9
  Built and verified static artifact

git diff --check
  passed (only the existing LF/CRLF working-copy warning)
```

### Limitations

- This fix round added deterministic API-boundary regression tests but did not repeat the prior real-browser acceptance or execute a new scientific training run.
- No acceptance evidence artifacts, production accounts, or secrets were created or used.
- Existing unrelated untracked paths `apps/web/report-v2.md`, `docs/superpowers/`, and `report-v2.md` were left untouched.
