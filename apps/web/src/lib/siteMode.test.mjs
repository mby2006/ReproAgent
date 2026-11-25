import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../../next.config.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
});

function loadConfig(siteMode) {
  const previous = process.env.NEXT_PUBLIC_SITE_MODE;
  if (siteMode === undefined) delete process.env.NEXT_PUBLIC_SITE_MODE;
  else process.env.NEXT_PUBLIC_SITE_MODE = siteMode;
  try {
    const loaded = { exports: {} };
    new Function("require", "module", "exports", outputText)(require, loaded, loaded.exports);
    return loaded.exports.default;
  } finally {
    if (previous === undefined) delete process.env.NEXT_PUBLIC_SITE_MODE;
    else process.env.NEXT_PUBLIC_SITE_MODE = previous;
  }
}

test("the default site mode builds a live server with prefetching enabled", () => {
  const config = loadConfig(undefined);
  assert.equal(config.output, undefined);
  assert.equal(config.cacheComponents, true);
  assert.equal(config.partialPrefetching, true);
});

test("static site mode exports HTML and disables server-only prefetching", () => {
  const config = loadConfig("static");
  assert.equal(config.output, "export");
  assert.equal(config.cacheComponents, undefined);
  assert.equal(config.partialPrefetching, undefined);
});

test("an invalid site mode fails the build instead of silently selecting live", () => {
  assert.throws(() => loadConfig("preview"), /NEXT_PUBLIC_SITE_MODE/);
});
