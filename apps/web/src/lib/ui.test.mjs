import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("./ui.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
});
const loaded = { exports: {} };
new Function("require", "module", "exports", outputText)(() => ({}), loaded, loaded.exports);

test("cancelled workflows are terminal and have a display label", () => {
  assert.equal(loaded.exports.isWorkflowTerminal("cancelled"), true);
  assert.equal(loaded.exports.isWorkflowTerminal("completed"), true);
  assert.equal(loaded.exports.isWorkflowTerminal("failed"), true);
  assert.equal(loaded.exports.isWorkflowTerminal("executing"), false);
  assert.equal(loaded.exports.WF_META.cancelled.label, "已取消");
});
