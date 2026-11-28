import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import { EVIDENCE_CATEGORIES, groupEvidenceItems } from "../../lib/evidence-index.mjs";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("./EvidenceIndex.tsx", import.meta.url), "utf8");
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
  if (name === "@/lib/api") return {
    apiFetch: async () => { throw new Error("tests must inject their own fetcher"); },
    apiDownload: async () => { throw new Error("tests must inject their own downloader"); },
    apiHref: (value) => value,
  };
  if (name === "@/lib/evidence-index.mjs") return { EVIDENCE_CATEGORIES, groupEvidenceItems };
  return require(name);
};
new Function("require", "module", "exports", outputText)(mockedRequire, loaded, loaded.exports);
const EvidenceIndex = loaded.exports.default;
const evidenceLocatorText = loaded.exports.evidenceLocatorText;
const loadPrivateEvidence = loaded.exports.loadPrivateEvidence;

function evidenceItem(overrides = {}) {
  return {
    id: "code:generated_file:file-1",
    category: "code",
    target_type: "generated_file",
    target_id: "file-1",
    locator: { attempt: 2, path: "train.py" },
    hash: null,
    unavailable_reason: null,
    ...overrides,
  };
}

test("plan and report placeholders without versions render their target id, never vundefined", () => {
  assert.equal(evidenceLocatorText(evidenceItem({
    target_type: "plan", target_id: "plan-7", locator: { plan_id: "plan-7" },
  })), "plan-7");
  assert.equal(evidenceLocatorText(evidenceItem({
    category: "review_report", target_type: "report", target_id: "report-9", locator: { report_id: "report-9" },
  })), "report-9");
});

test("versionless plan and report placeholders remain readable in rendered markup", () => {
  const index = {
    run_id: "run-1",
    items: [
      evidenceItem({ id: "plan-placeholder", category: "plan", target_type: "plan", target_id: "plan-7", locator: { plan_id: "plan-7" }, unavailable_reason: "版本不可用" }),
      evidenceItem({ id: "report-placeholder", category: "review_report", target_type: "report", target_id: "report-9", locator: { report_id: "report-9" }, unavailable_reason: "版本不可用" }),
      evidenceItem({ id: "trace-placeholder", category: "review_report", target_type: "review_trace", target_id: "report-9", locator: { source: "plan" }, unavailable_reason: "Plan 审核记录不可用" }),
    ],
  };
  const html = renderToStaticMarkup(React.createElement(EvidenceIndex, { index }));
  assert.ok(!html.includes("vundefined"));
  assert.ok(!html.includes("undefined"));
  assert.ok(html.includes("plan-7"));
  assert.ok(html.includes("report-9"));
  assert.ok(html.includes("plan · report-9"));
});

test("legacy review trace placeholders without a source render their target id", () => {
  assert.equal(evidenceLocatorText(evidenceItem({
    category: "review_report", target_type: "review_trace", target_id: "report-9", locator: {},
  })), "report-9");
});

test("private code interaction loads the exact indexed attempt and matching file", async () => {
  const calls = [];
  const panel = await loadPrivateEvidence({
    runId: "run-1",
    item: evidenceItem(),
    fetchEvidence: async (path) => {
      calls.push(path);
      return [{ path: "train.py", content: "final attempt code" }];
    },
  });
  assert.deepEqual(calls, ["/v1/runs/run-1/files?attempt=2"]);
  assert.deepEqual(panel, { title: "train.py", content: "final attempt code" });
});

test("private code interaction reports a missing indexed file instead of showing another file", async () => {
  await assert.rejects(
    () => loadPrivateEvidence({
      runId: "run-1",
      item: evidenceItem(),
      fetchEvidence: async () => [{ path: "old.py", content: "wrong attempt" }],
    }),
    /代码文件不可用/,
  );
});

test("private report interaction reads the exact immutable report version", async () => {
  const calls = [];
  const panel = await loadPrivateEvidence({
    runId: "run-1",
    item: evidenceItem({
      id: "review_report:report:report-3:v3",
      category: "review_report",
      target_type: "report",
      target_id: "report-3",
      locator: { report_id: "report-3", version: 3 },
    }),
    fetchEvidence: async (path) => {
      calls.push(path);
      return { markdown: "# immutable report" };
    },
  });
  assert.deepEqual(calls, ["/v1/runs/run-1/report?version=3"]);
  assert.deepEqual(panel, { title: "报告 v3", content: "# immutable report" });
});

test("private report interaction propagates API failures to the UI error boundary", async () => {
  await assert.rejects(
    () => loadPrivateEvidence({
      runId: "run-1",
      item: evidenceItem({ target_type: "report", locator: { version: 3 } }),
      fetchEvidence: async () => { throw new Error("REPORT_VERSION_NOT_FOUND"); },
    }),
    /REPORT_VERSION_NOT_FOUND/,
  );
});

test("private artifact interaction downloads the exact indexed artifact", async () => {
  const calls = [];
  const result = await loadPrivateEvidence({
    runId: "run-1",
    item: evidenceItem({
      id: "execution:artifact:artifact-4",
      category: "execution",
      target_type: "artifact",
      target_id: "artifact-4",
      locator: { artifact_id: "artifact-4", name: "metrics.json" },
    }),
    downloadArtifact: async (path, name) => { calls.push([path, name]); },
  });
  assert.equal(result, null);
  assert.deepEqual(calls, [["/v1/runs/run-1/artifacts/artifact-4", "metrics.json"]]);
});

test("private artifact interaction propagates denial failures without opening a panel", async () => {
  await assert.rejects(
    () => loadPrivateEvidence({
      runId: "run-1",
      item: evidenceItem({ target_type: "artifact", target_id: "artifact-4", locator: { name: "metrics.json" } }),
      downloadArtifact: async () => { throw new Error("RUN_NOT_FOUND"); },
    }),
    /RUN_NOT_FOUND/,
  );
});
