// Run with node --test; effects are intentionally not run during static rendering.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("./ReportPanel.tsx", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: {
    jsx: ts.JsxEmit.ReactJSX,
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
  },
});
const loaded = { exports: {} };
let states = [];
const mockedRequire = (name) => {
  if (name === "react") return {
    ...React,
    useState: (initial) => {
      const value = states.length ? states.shift() : typeof initial === "function" ? initial() : initial;
      return [value, () => {}];
    },
    useCallback: (callback) => callback,
    useEffect: () => {},
  };
  if (name === "@/lib/api") return {
    apiFetch: () => { throw new Error("render must not fetch"); },
    apiHref: (value) => value,
    ApiError: class ApiError extends Error {},
  };
  if (name === "@/lib/ui") return {
    CHECK_META: {}, FINAL_META: { METHOD_VALIDATED: { tone: "ok", label: "方法验证" } },
    TONE_BADGE: {}, fmtDate: (value) => value, fmtNum: String,
  };
  if (name === "@/lib/fieldLabels") return {
    fieldLabel: (value) => value, missingGloss: () => "", shortValue: String,
  };
  if (name.includes("icons")) return { IconDownload: () => null, IconRefresh: () => null, IconWarn: () => null };
  if (name === "@/components/ParameterTable" || name === "@/components/MetricChart") {
    return { default: () => null };
  }
  if (name.startsWith("@/")) return {};
  return require(name);
};
new Function("require", "module", "exports", outputText)(mockedRequire, loaded, loaded.exports);
const ReportPanel = loaded.exports.default;
const reportDownloadName = loaded.exports.reportDownloadName;

const report = {
  id: "report-3",
  run_id: "run-1",
  version: 3,
  execution_attempt: 2,
  generated_at: "2026-01-01T00:00:00Z",
  created_at: "2026-01-01T00:00:00Z",
  markdown: "# final report",
  report_json: {
    references: {
      paper_id: "paper-1", spec_version_id: "spec-1", spec_version: 1,
      experiment_id: "experiment-1", plan_id: "plan-2", plan_version: 2,
      code_version: 2, code_hash: "hash", run_id: "run-1", generated_file_ids: [],
      workflow_id: "workflow-1", execution_attempt: 2,
      execution_request_id: "request-1", runner_run_id: "runner-1",
    },
    final_status: "METHOD_VALIDATED",
    summary: "summary",
    method_checks: [], experiment_checks: [], metric_comparisons: [],
    scaling_deviations: [], missing_information: [], assumptions: [], limitations: [],
  },
};

test("report header exposes the selected immutable version and final attempt", () => {
  states = [report, "latest", 3, false, false, false, null, false, null];
  const html = renderToStaticMarkup(React.createElement(ReportPanel, {
    runId: "run-1", onFile: () => {},
  }));
  assert.match(html, /报告 v3 · 执行尝试 2/);
  assert.match(html, /aria-label="选择报告版本"/);
  assert.match(html, /<option value="latest"/);
  assert.match(html, /<option value="3"/);
});

test("markdown download names the exact displayed run and report version", () => {
  assert.equal(reportDownloadName("run-1", 3), "report-run-run-1-v3.md");
});
