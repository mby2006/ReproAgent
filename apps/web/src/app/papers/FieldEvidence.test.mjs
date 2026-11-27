// Run with node --test; effects are intentionally not run during static rendering.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("./FieldEvidence.tsx", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
});
const loaded = { exports: {} };
new Function("require", "module", "exports", outputText)(require, loaded, loaded.exports);
const { FieldEvidence } = loaded.exports;
const render = (field) => renderToStaticMarkup(React.createElement(FieldEvidence, { field }));

const base = {
  value: "MNIST", status: "inferred", source_page: null, source_section: null,
  evidence_quote: null, confidence: 0.7, human_confirmed: false,
};

test("rejected citation renders candidate context as unverified review material", () => {
  const html = render({
    ...base,
    grounding_status: "located_unsupported",
    candidate_quote: "NIST Special Database 3",
    candidate_page: 10,
  });
  assert.match(html, /候选定位/);
  assert.match(html, /未通过语义核验/);
  assert.match(html, /NIST Special Database 3/);
  assert.match(html, /p\.10/);
});

test("citation-not-found keeps the candidate quote without inventing a page", () => {
  const html = render({
    ...base,
    grounding_status: "citation_not_found",
    candidate_quote: "Twenty iterations",
    candidate_page: null,
  });
  assert.match(html, /候选定位/);
  assert.match(html, /Twenty iterations/);
  assert.match(html, /候选页：未定位/);
});

test("accepted evidence remains distinct from candidate review context", () => {
  const html = render({
    ...base,
    grounding_status: "verified",
    source_page: 2,
    evidence_quote: "MNIST database",
    candidate_quote: "rejected candidate",
    candidate_page: 10,
  });
  assert.match(html, /MNIST database/);
  assert.doesNotMatch(html, /候选定位|rejected candidate/);
});
