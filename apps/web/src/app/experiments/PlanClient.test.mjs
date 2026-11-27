import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import React from "react";
import ts from "typescript";
import { selectConditionSummary } from "../../lib/conditions.mjs";

const require = createRequire(import.meta.url);
class ApiError extends Error {
  constructor(message, status, code) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

const source = readFileSync(new URL("./PlanClient.tsx", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    jsx: ts.JsxEmit.ReactJSX,
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
    esModuleInterop: true,
  },
});
const loaded = { exports: {} };
const mockedRequire = (name) => {
  if (name === "react") return React;
  if (name === "next/navigation") return {};
  if (name === "@/lib/api") return {
    ApiError,
    apiFetch: async () => { throw new Error("tests must inject their own fetcher"); },
  };
  if (name === "@/lib/conditions.mjs") return { selectConditionSummary };
  if (name === "@/lib/ui") return { RISK_META: {}, SOURCE_META: {}, TONE_BADGE: {}, fmtDate: String, tierOf: () => "routine" };
  if (name.startsWith("@/") || name === "./RunPanel") return {};
  return require(name);
};
new Function("require", "module", "exports", outputText)(mockedRequire, loaded, loaded.exports);

const { conditionRowsForPlan, loadConditionReport } = loaded.exports;

function paperParam(value, extra = {}) {
  return {
    value,
    source: "paper_explicit",
    paper_value: value,
    evidence_quote: `paper states ${value}`,
    source_page: 3,
    rationale: null,
    requires_confirmation: false,
    human_confirmed: false,
    confirmed_by: null,
    ...extra,
  };
}

function makePlan(overrides = {}) {
  return {
    delegated_at: null,
    delegated_by: null,
    scaling_profile: "mvp",
    rationale: "test plan",
    dataset_config: { name: "MNIST", source: "paper_explicit" },
    model_config: { architecture: "LeNet-5", source: "paper_explicit", description: null },
    training_config: {
      optimizer: paperParam("sgd"),
      learning_rate: paperParam(0.1),
      loss_function: paperParam("cross_entropy"),
      batch_size: paperParam(64),
      epochs: paperParam(10),
      seed: 42,
    },
    evaluation_config: {
      metrics: ["accuracy"],
      target_metric: "accuracy",
      reported_value: 99.1,
      reported_condition: "test",
      reported_source_page: 7,
      tolerance: 0.01,
      comparison_note: "MVP comparison only",
    },
    ...overrides,
  };
}

function makeRun(id, planVersion, createdAt) {
  return {
    id,
    experiment_id: "experiment-1",
    plan_id: `plan-${planVersion}`,
    code_version: 1,
    plan_version: planVersion,
    status: "succeeded",
    repair_attempt: 0,
    exit_code: 0,
    error_category: null,
    created_at: createdAt,
    started_at: createdAt,
    finished_at: createdAt,
    updated_at: createdAt,
  };
}

function makeReport(runId, planVersion, version, actual = "adam") {
  return {
    id: `report-${version}`,
    run_id: runId,
    version,
    execution_attempt: 2,
    generation_ms: 100,
    content_sha256: "hash",
    generated_at: "2026-01-01T00:00:00Z",
    created_at: "2026-01-01T00:00:00Z",
    markdown: "# report",
    report_json: {
      reader_summary: null,
      target_metric: "accuracy",
      parameter_comparisons: [{
        key: "optimizer",
        label: "优化器",
        paper: "SGD",
        plan: "sgd",
        actual,
        paper_evidence: { kind: "paper", source_page: 3, quote: "SGD" },
        actual_evidence: { kind: "config", field_path: "training_config.optimizer" },
        decision: null,
        note: "optimizer comparison",
      }],
      review_mode: null,
      delegated_at: null,
      delegated_by: null,
      review_decisions: [],
      review_disclaimer: null,
      references: {
        paper_id: "paper-1",
        spec_version_id: "spec-1",
        spec_version: 1,
        experiment_id: "experiment-1",
        plan_id: `plan-${planVersion}`,
        plan_version: planVersion,
        code_version: 1,
        code_hash: "hash",
        run_id: runId,
        workflow_id: "workflow-1",
        execution_attempt: 2,
        execution_request_id: "request-1",
        runner_run_id: "runner-1",
        generated_file_ids: [],
        review_trace_ids: [],
      },
      final_status: "METHOD_VALIDATED",
      summary: "summary",
      method_checks: [],
      experiment_checks: [],
      metric_comparisons: [],
      scaling_deviations: [],
      missing_information: [],
      assumptions: [],
      limitations: [],
    },
  };
}

function scriptedFetcher(runs, reportOutcomes) {
  const calls = [];
  return {
    calls,
    fetcher: async (path) => {
      calls.push(path);
      if (path === "/v1/experiments/experiment-1/runs") return runs;
      const runId = path.match(/^\/v1\/runs\/([^/]+)\/report$/)?.[1];
      const outcome = reportOutcomes[runId];
      if (outcome instanceof Error) throw outcome;
      return outcome;
    },
  };
}

test("inferred plan candidates are not rendered as paper evidence", () => {
  assert.equal(typeof conditionRowsForPlan, "function", "plan condition rows must be testable");
  const plan = makePlan();
  plan.training_config.optimizer = {
    value: "adam",
    source: "inferred",
    paper_value: "adam",
    evidence_quote: "candidate quote from understanding",
    source_page: 9,
    rationale: "理解阶段推断",
    requires_confirmation: true,
    human_confirmed: false,
    confirmed_by: null,
  };
  const rows = conditionRowsForPlan(plan, { status: "unavailable" }, 2);
  const optimizer = rows.find((row) => row.key === "optimizer");
  assert.equal(optimizer.paper, "无可用证据");
  assert.equal(optimizer.paper_evidence, null);
  assert.equal(optimizer.source, "无可用证据");
  assert.equal(optimizer.plan, "adam");
  assert.equal(optimizer.actual, "尚未执行");
});

test("paper-explicit plan parameters retain their paper value and citation", () => {
  assert.equal(typeof conditionRowsForPlan, "function", "plan condition rows must be testable");
  const rows = conditionRowsForPlan(makePlan(), { status: "unavailable" }, 2);
  const optimizer = rows.find((row) => row.key === "optimizer");
  assert.equal(optimizer.paper, "sgd");
  assert.deepEqual(optimizer.paper_evidence, {
    kind: "paper",
    source_page: 3,
    quote: "paper states sgd",
    detail: "sgd",
  });
});

test("target metric without provenance is not promoted to paper evidence", () => {
  assert.equal(typeof conditionRowsForPlan, "function", "plan condition rows must be testable");
  const rows = conditionRowsForPlan(makePlan(), { status: "unavailable" }, 2);
  const target = rows.find((row) => row.key === "target_metric");
  assert.equal(target.paper, "无可用证据");
  assert.equal(target.paper_evidence, null);
  assert.equal(target.plan, "accuracy");
});

test("newest unavailable report falls through to an older report for the current plan", async () => {
  assert.equal(typeof loadConditionReport, "function", "report selection must be testable");
  const newest = makeRun("run-new", 2, "2026-01-02T00:00:00Z");
  const older = makeRun("run-old", 2, "2026-01-01T00:00:00Z");
  const olderReport = makeReport("run-old", 2, 4);
  const { fetcher, calls } = scriptedFetcher([newest, older], {
    "run-new": new ApiError("missing", 404, "REPORT_UNAVAILABLE"),
    "run-old": olderReport,
  });
  const state = await loadConditionReport("experiment-1", 2, fetcher);
  assert.deepEqual(state, { status: "ready", report: olderReport });
  assert.deepEqual(calls, [
    "/v1/experiments/experiment-1/runs",
    "/v1/runs/run-new/report",
    "/v1/runs/run-old/report",
  ]);
  const rows = conditionRowsForPlan(makePlan(), state, 2);
  assert.equal(rows.find((row) => row.key === "optimizer").actual, "adam");
});

test("newest report generation failure falls through to an older current-plan report", async () => {
  assert.equal(typeof loadConditionReport, "function", "report selection must be testable");
  const newest = makeRun("run-new", 2, "2026-01-02T00:00:00Z");
  const older = makeRun("run-old", 2, "2026-01-01T00:00:00Z");
  const olderReport = makeReport("run-old", 2, 5);
  const { fetcher } = scriptedFetcher([newest, older], {
    "run-new": new ApiError("generation failed", 409, "REPORT_GENERATION_FAILED"),
    "run-old": olderReport,
  });
  const state = await loadConditionReport("experiment-1", 2, fetcher);
  assert.deepEqual(state, { status: "ready", report: olderReport });
});

test("report generation failure does not render the fabricated not-executed state", async () => {
  assert.equal(typeof loadConditionReport, "function", "report selection must be testable");
  const run = makeRun("run-1", 2, "2026-01-01T00:00:00Z");
  const { fetcher } = scriptedFetcher([run], {
    "run-1": new ApiError("generation failed", 409, "REPORT_GENERATION_FAILED"),
  });
  const state = await loadConditionReport("experiment-1", 2, fetcher);
  assert.equal(state.status, "unknown");
  const rows = conditionRowsForPlan(makePlan(), state, 2);
  assert.ok(rows.every((row) => row.actual === "无法确认"));
  assert.ok(rows.every((row) => row.actual !== "尚未执行"));
});

test("network-style run loading failure does not render the not-executed state", async () => {
  assert.equal(typeof loadConditionReport, "function", "report selection must be testable");
  const state = await loadConditionReport("experiment-1", 2, async () => {
    throw new TypeError("Failed to fetch");
  });
  assert.equal(state.status, "unknown");
  const rows = conditionRowsForPlan(makePlan(), state, 2);
  assert.ok(rows.every((row) => row.actual === "无法确认"));
  assert.ok(rows.every((row) => row.actual !== "尚未执行"));
});

test("an older report for a different plan version is not used", async () => {
  assert.equal(typeof loadConditionReport, "function", "report selection must be testable");
  const newest = makeRun("run-new", 2, "2026-01-02T00:00:00Z");
  const older = makeRun("run-old", 1, "2026-01-01T00:00:00Z");
  const wrongVersionReport = makeReport("run-old", 1, 6, "must-not-render");
  const { fetcher } = scriptedFetcher([newest, older], {
    "run-new": new ApiError("missing", 404, "REPORT_UNAVAILABLE"),
    "run-old": wrongVersionReport,
  });
  const state = await loadConditionReport("experiment-1", 2, fetcher);
  assert.equal(state.status, "unknown");
  const rows = conditionRowsForPlan(makePlan(), state, 2);
  assert.ok(rows.every((row) => row.actual === "无法确认"));
  assert.ok(rows.every((row) => String(row.actual) !== "must-not-render"));
});

test("only a confirmed no-report state renders not executed", async () => {
  assert.equal(typeof loadConditionReport, "function", "report selection must be testable");
  const empty = await loadConditionReport("experiment-1", 2, async () => []);
  assert.deepEqual(empty, { status: "unavailable" });
  assert.ok(conditionRowsForPlan(makePlan(), empty, 2).every((row) => row.actual === "尚未执行"));

  const run = makeRun("run-1", 2, "2026-01-01T00:00:00Z");
  const { fetcher } = scriptedFetcher([run], {
    "run-1": new ApiError("missing", 404, "REPORT_UNAVAILABLE"),
  });
  const missing = await loadConditionReport("experiment-1", 2, fetcher);
  assert.deepEqual(missing, { status: "unavailable" });
  assert.ok(conditionRowsForPlan(makePlan(), missing, 2).every((row) => row.actual === "尚未执行"));
});

test("a ready report for a different plan version is not rendered after a version switch", () => {
  assert.equal(typeof conditionRowsForPlan, "function", "plan condition rows must be testable");
  const staleReport = makeReport("run-old", 1, 7, "stale-actual");
  const rows = conditionRowsForPlan(makePlan(), { status: "ready", report: staleReport }, 2);
  assert.ok(rows.every((row) => row.actual === "无法确认"));
  assert.ok(rows.every((row) => row.actual !== "stale-actual"));
});

test("loading and ready-without-comparisons states do not claim execution", () => {
  assert.equal(typeof conditionRowsForPlan, "function", "plan condition rows must be testable");
  const loading = conditionRowsForPlan(makePlan(), { status: "loading" }, 2);
  assert.ok(loading.every((row) => row.actual === "正在确认"));
  const ready = conditionRowsForPlan(makePlan(), { status: "ready", report: makeReport("run-1", 2, 1) }, 2);
  assert.equal(ready.find((row) => row.key === "optimizer").actual, "adam");
  const readyWithoutComparisons = makeReport("run-1", 2, 1);
  readyWithoutComparisons.report_json.parameter_comparisons = [];
  const noComparisons = conditionRowsForPlan(makePlan(), { status: "ready", report: readyWithoutComparisons }, 2);
  assert.ok(noComparisons.every((row) => row.actual === "无可用证据"));
});
