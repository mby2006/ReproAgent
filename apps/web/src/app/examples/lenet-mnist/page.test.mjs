// Run with node --test; effects are intentionally not run during static rendering.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import { selectConditionSummary } from "../../../lib/conditions.mjs";
import { buildPublicEvidenceIndex } from "../../../lib/evidence-index.mjs";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("./page.tsx", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
});
const parameterTableSource = readFileSync(new URL("../../../components/ParameterTable.tsx", import.meta.url), "utf8");
const parameterTableOutput = ts.transpileModule(parameterTableSource, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
}).outputText;
const parameterTableModule = { exports: {} };
new Function("require", "module", "exports", parameterTableOutput)(require, parameterTableModule, parameterTableModule.exports);
const json = (name) => JSON.parse(readFileSync(new URL(`../../../../public/examples/lenet/${name}.json`, import.meta.url), "utf8"));
const loaded = { exports: {} };
const mockedRequire = (name) => {
  if (name === "@/components/SiteLink") return { __esModule: true, default: () => null };
  if (name === "@/components/PredictionReader") return { PredictionReader: () => null };
  if (name === "@/components/PublicHeader") return { __esModule: true, default: () => null };
  if (name === "@/components/ParameterTable") return parameterTableModule.exports;
  if (name === "@/lib/conditions.mjs") return { selectConditionSummary };
  if (name === "@/lib/evidence-index.mjs") return { buildPublicEvidenceIndex, groupEvidenceItems: (items) => Object.groupBy(items, (item) => item.category) };
  if (name === "@/app/experiments/EvidenceIndex") return { __esModule: true, default: () => null };
  if (name === "@/lib/site") return { isStaticSite: true };
  if (name.endsWith("metrics.json")) return { __esModule: true, default: json("metrics") };
  if (name.endsWith("prediction_examples.json")) return { __esModule: true, default: json("prediction_examples") };
  if (name.endsWith("conditions.json")) return { __esModule: true, default: json("conditions") };
  if (name.endsWith("manifest.json")) return { __esModule: true, default: json("manifest") };
  if (name.startsWith("@/")) return {};
  return require(name);
};
new Function("require", "module", "exports", outputText)(mockedRequire, loaded, loaded.exports);
const LeNetExample = loaded.exports.default;

test("public archive page exposes all nine verified files", () => {
  const html = renderToStaticMarkup(React.createElement(LeNetExample));
  const manifest = json("manifest");
  assert.equal(Object.keys(manifest.files).length, 9);
  for (const file of Object.keys(manifest.files)) {
    assert.ok(html.includes(`href="/examples/lenet/${file}"`), file);
  }
  assert.match(html, /九个归档文件/);
});

test("public archive shows the condition summary before the collapsed full table", () => {
  const html = renderToStaticMarkup(React.createElement(LeNetExample));
  const summaryIndex = html.indexOf("论文条件 / 批准计划 / 实际实现");
  const detailsIndex = html.indexOf('<details id="evidence"');
  const fullTableIndex = html.indexOf("逐项核查实验条件");
  assert.ok(summaryIndex >= 0, "condition summary is visible");
  assert.ok(detailsIndex > summaryIndex, "summary appears before the evidence disclosure");
  assert.ok(fullTableIndex > detailsIndex, "full five-column table remains in the disclosure");
  for (const text of ["LeNet-5", "sgd", "11.23%"]) assert.ok(html.includes(text), text);
});

test("public archive source builds a local five-category evidence index", () => {
  assert.match(source, /buildPublicEvidenceIndex/);
  assert.match(source, /EvidenceIndex/);
  assert.doesNotMatch(source, /apiFetch|supabase|fetch\(/);
});
