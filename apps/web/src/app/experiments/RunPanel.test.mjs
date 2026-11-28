// Run with node --test; effects are intentionally not run during static rendering.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("./RunPanel.tsx", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
});
const loaded = { exports: {} };
let states = [];
let repairCeiling = 3;
const mockedRequire = (name) => {
  if (name === "react") return {
    ...React,
    useState: (initial) => {
      const value = states.length ? states.shift() : typeof initial === "function" ? initial() : initial;
      return [value, () => {}];
    },
    useCallback: (callback) => callback,
    useEffect: () => {},
    useRef: (value) => ({ current: value }),
    createContext: React.createContext,
    useContext: React.useContext,
  };
  if (name === "@/lib/api") return { apiFetch: () => { throw new Error("render must not fetch"); } };
  if (name === "@/components/PredictionReader") return { PrivatePredictions: () => null };
  if (name === "./ReportPanel") return { default: () => null };
  if (name === "./EvidenceIndex") return { default: () => null };
  if (name.includes("icons")) return { IconPlay: () => null, IconStop: () => null };
  if (name === "@/lib/ui") return {
    ERROR_LABEL: {}, RUN_META: { running: { tone: "info", label: "运行中" } },
    get MAX_REPAIR_ATTEMPTS() { return repairCeiling; },
    isWorkflowTerminal: (status) => ["completed", "failed", "cancelled"].includes(status),
    RUN_TERMINAL: ["succeeded", "failed", "timeout", "cancelled"],
    TONE_BADGE: {}, fmtDate: (value) => value,
  };
  if (name.startsWith("@/")) return {};
  return require(name);
};
new Function("require", "module", "exports", outputText)(mockedRequire, loaded, loaded.exports);
const RunPanel = loaded.exports.default;
const findNewRun = loaded.exports.findNewRun;

test("workflow runs are selected only when they appear after the initial history snapshot", () => {
  assert.equal(findNewRun([run], null), undefined);
  assert.equal(findNewRun([run], new Set([run.id])), undefined);
  assert.equal(findNewRun([run, { ...run, id: "run-2" }], new Set([run.id]))?.id, "run-2");
});

const run = {
  id: "run-1", experiment_id: "experiment-1", status: "running", code_version: 1,
  plan_version: 1, repair_attempt: 0, exit_code: null, error_category: null,
  created_at: "2026-01-01T00:00:00Z", error: null, code_hash: null,
  runner_run_id: null, metrics_json: {}, audit_json: {}, runtime_json: {},
  stdout_path: null, stderr_path: null,
};

test("run output tabs expose tablist, selected, controls, and tabpanel semantics", () => {
  states = [[run], "run-1", run, { stdout: "hello", stderr: "" }, [], [], null, "stdout", null, null, false, false, null];
  const html = renderToStaticMarkup(React.createElement(RunPanel, {
    experimentId: "experiment-1", approved: true,
  }));
  assert.ok(html.includes('role="tablist"'));
  assert.ok(html.includes('role="tab"'));
  assert.ok(html.includes('aria-selected="true"'));
  assert.ok(html.includes('aria-controls="run-output-panel"'));
  assert.ok(html.includes('role="tabpanel"'));
});

test("empty run history remains lazy and announces loading state", () => {
  states = [[run]];
  const html = renderToStaticMarkup(React.createElement(RunPanel, {
    experimentId: "experiment-1", approved: false,
  }));
  assert.ok(html.includes('aria-label="选择运行"'));
  assert.ok(html.includes('role="status"'));
  assert.ok(!html.includes('role="tablist"'));
});

test("run failures and an in-flight cancellation are announced accessibly", () => {
  states = [[run], "run-1", run, { stdout: "", stderr: "failed" }, [], [], null, "stderr", null, "启动失败", false, true, null];
  const html = renderToStaticMarkup(React.createElement(RunPanel, {
    experimentId: "experiment-1", approved: true,
  }));
  assert.ok(html.includes('role="alert"'));
  assert.match(html, /<button[^>]*disabled=""[^>]*aria-busy="true"[^>]*>.*取消中/s);
});

test("repair chip takes its ceiling from the shared ui declaration", () => {
  states = [[run], "run-1", { ...run, repair_attempt: 2 }, { stdout: "", stderr: "" },
    [], [], null, "stdout", null, null, false, false, null];
  repairCeiling = 9;
  try {
    const html = renderToStaticMarkup(React.createElement(RunPanel, {
      experimentId: "experiment-1", approved: true,
    }));
    assert.match(html, /repair 2(?:<!-- -->)?\/9/);
    assert.ok(!/repair 2(?:<!-- -->)?\/3/.test(html));
  } finally {
    repairCeiling = 3;
  }
});
