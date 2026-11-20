// Run with node --test; no browser, database, or added test dependencies.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
function loadComponent(relativePath) {
  const source = readFileSync(new URL(relativePath, import.meta.url), "utf8");
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
  });
  const loaded = { exports: {} };
  const mockedRequire = (name) => {
    if (name.includes("icons")) return { IconChevronDown: () => null, IconWarn: () => null };
    if (name.startsWith("@/")) return {};
    return require(name);
  };
  new Function("require", "module", "exports", outputText)(mockedRequire, loaded, loaded.exports);
  return loaded.exports;
}

const { TraceRow } = loadComponent("./WorkflowInsights.tsx");
const { ReportReview, ReviewAudit } = loadComponent("../app/experiments/[id]/ReportPanel.tsx");
const render = (component, props) => renderToStaticMarkup(React.createElement(component, props));
const disclaimer = "本实验关键假设由系统自动接受，未经人工逐项核验；结果仅供趋势参考。";
const decision = {
  id: "review-event-1", field_path: "training_config.optimizer", before: "sgd", after: "adam",
  confirmed_by: "auto", review_mode: "delegated", actor: "actor-id", timestamp: "2026-01-02T12:00:00Z",
  recommendation_reasoning: "[REDACTED] recommended", recommendation_confidence: 0.95,
};

test("delegated report displays the exact warning and real authorizer/time", () => {
  const html = render(ReportReview, { data: {
    review_mode: "delegated", delegated_by: "authorizer-id", delegated_at: decision.timestamp,
    assumptions: [], review_decisions: [decision],
  } });
  assert.ok(html.includes(disclaimer));
  for (const text of ["托管审核", "authorizer-id", decision.timestamp, "审核来源声明"]) assert.ok(html.includes(text));
});

test("historical automatic events still warn after switching to expert", () => {
  const html = render(ReportReview, { data: {
    review_mode: "expert", assumptions: [], review_decisions: [decision], review_disclaimer: "ordinary",
  } });
  assert.ok(html.includes("专家审核"));
  assert.ok(html.includes(disclaimer));
  assert.ok(!html.includes("ordinary"));
});

test("legacy reports do not fabricate review provenance", () => {
  const html = render(ReportReview, { data: { assumptions: [] } });
  assert.ok(html.includes("unknown"));
  assert.ok(html.includes("不能认定已人工逐项核验"));
  assert.ok(!html.includes("托管授权用户"));
});

test("review audit keeps stable anchor IDs and readable values/source/metadata", () => {
  const html = render(ReviewAudit, { decisions: [decision] });
  for (const text of [
    'id="review-review-event-1"', "training_config.optimizer", "sgd", "adam",
    "系统自动接受，非人工确认", "actor-id", decision.timestamp, "0.95", "[REDACTED]",
  ]) assert.ok(html.includes(text), text);
  assert.ok(!html.includes(" open="));
});

test("human, AI-assisted, automatic and unconfirmed sources remain distinct", () => {
  for (const [source, text] of [
    ["human", "human · 人工确认"], ["ai_assisted", "ai_assisted · AI 辅助，人工采纳"],
    ["auto", "auto · 系统自动接受，非人工确认"], [null, "未确认"],
  ]) assert.ok(render(ReviewAudit, { decisions: [{ ...decision, confirmed_by: source }] }).includes(text));
});

test("workflow review decisions render Chinese audit fields rather than raw JSON", () => {
  const html = render(TraceRow, { trace: {
    id: decision.id, node_name: "review_decision", input_json: { field_path: decision.field_path, before: "sgd" },
    output_json: decision, created_at: decision.timestamp, duration_ms: null, error: null, retry_count: 0,
  } });
  for (const text of ["审核决策", "确认来源", "托管审核", "系统自动接受（非人工确认）", "sgd", "adam", "推荐理由"]) assert.ok(html.includes(text));
  assert.ok(!html.includes("<pre"));
});

test("ordinary workflow node traces retain JSON and timing details", () => {
  const html = render(TraceRow, { trace: {
    id: "ordinary", node_name: "execute", input_json: { attempt: 1 }, output_json: { ok: true },
    created_at: decision.timestamp, duration_ms: 120, error: null, retry_count: 0,
  } });
  assert.ok(html.includes("execute"));
  assert.ok(html.includes("120ms"));
  assert.ok(html.includes("<pre"));
});
