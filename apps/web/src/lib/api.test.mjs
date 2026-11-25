import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import ts from "typescript";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("./api.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
});
const loaded = { exports: {} };
let userId = "user-a";
const keys = [];
const mockedRequire = (name) => {
  if (name === "./supabase") return {
    supabase: { auth: { getSession: async () => ({ data: { session: { user: { id: userId }, access_token: userId } } }) } },
  };
  if (name === "./polling") return {
    readOnce: async (key, request) => { keys.push(key); return request(); },
  };
  return require(name);
};
new Function("require", "module", "exports", outputText)(mockedRequire, loaded, loaded.exports);

test("coalesced reads never cross authenticated users", async () => {
  globalThis.fetch = async () => new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } });
  await loaded.exports.apiGet("/v1/workflows");
  userId = "user-b";
  await loaded.exports.apiGet("/v1/workflows");
  assert.equal(keys.length, 2);
  assert.notEqual(keys[0], keys[1]);
});
