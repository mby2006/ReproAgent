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

test("structured detail yields a stable ApiError code separate from status and message", async () => {
  globalThis.fetch = async () => new Response(
    JSON.stringify({ detail: { code: "REPORT_UNAVAILABLE", message: "报告尚未生成" } }),
    { status: 404, statusText: "Not Found", headers: { "Content-Type": "application/json" } },
  );
  await assert.rejects(
    () => loaded.exports.apiFetch("/v1/runs/run-1/report"),
    (err) => {
      assert.equal(err.name, "ApiError");
      assert.equal(err.code, "REPORT_UNAVAILABLE");
      assert.equal(err.status, 404);
      assert.equal(err.message, "报告尚未生成");
      return true;
    },
  );
});

test("string detail still maps to UNKNOWN_ERROR", async () => {
  globalThis.fetch = async () => new Response(
    JSON.stringify({ detail: "报告尚未生成" }),
    { status: 404, statusText: "Not Found", headers: { "Content-Type": "application/json" } },
  );
  await assert.rejects(
    () => loaded.exports.apiFetch("/v1/runs/run-1/report"),
    (err) => {
      assert.equal(err.code, "UNKNOWN_ERROR");
      assert.equal(err.status, 404);
      assert.equal(err.message, "报告尚未生成");
      return true;
    },
  );
});

test("control flow reads the code, never the Chinese message text", async () => {
  const request = (body, status) => async () => new Response(
    JSON.stringify({ detail: body }), { status, statusText: "Conflict", headers: { "Content-Type": "application/json" } },
  );
  // Same code with different display texts must classify identically.
  const codes = [];
  for (const [body, status] of [
    [{ code: "REPORT_GENERATION_FAILED", message: "报告生成失败: upstream 500" }, 502],
    [{ code: "REPORT_GENERATION_FAILED", message: "文案完全换一种说法" }, 502],
    [{ code: "REPORT_NOT_READY", message: "运行尚未结束，无法生成报告" }, 409],
  ]) {
    globalThis.fetch = request(body, status);
    try {
      await loaded.exports.apiFetch("/v1/runs/run-1/report", { method: "POST" });
      assert.fail("expected rejection");
    } catch (err) {
      assert.equal(err.name, "ApiError");
      codes.push(err.code);
    }
  }
  assert.deepEqual(codes, ["REPORT_GENERATION_FAILED", "REPORT_GENERATION_FAILED", "REPORT_NOT_READY"]);
});

test("missing or unparseable body falls back to status text with UNKNOWN_ERROR", async () => {
  globalThis.fetch = async () => new Response("", { status: 503, statusText: "Service Unavailable" });
  await assert.rejects(
    () => loaded.exports.apiFetch("/v1/workflows/wf-1/resume", { method: "POST" }),
    (err) => {
      assert.equal(err.code, "UNKNOWN_ERROR");
      assert.equal(err.status, 503);
      assert.equal(err.message, "Service Unavailable");
      return true;
    },
  );
});

test("cancelWorkflow posts to the workflow cancel endpoint", async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), method: init?.method });
    return new Response(JSON.stringify({ workflow_id: "wf-1", status: "cancelled" }),
      { status: 200, headers: { "Content-Type": "application/json" } });
  };
  const result = await loaded.exports.cancelWorkflow("wf-1");
  assert.equal(calls[0].method, "POST");
  assert.ok(calls[0].url.endsWith("/v1/workflows/wf-1/cancel"));
  assert.equal(result.status, "cancelled");
});

test("apiDownload authenticates the request and clicks a named object-url download", async () => {
  userId = "user-a";
  const original = {
    fetch: globalThis.fetch,
    document: globalThis.document,
    createObjectURL: URL.createObjectURL,
    revokeObjectURL: URL.revokeObjectURL,
    setTimeout: globalThis.setTimeout,
  };
  const calls = [];
  const link = { href: "", download: "", click: () => calls.push(["click", link.href, link.download]) };
  globalThis.fetch = async (url, init) => {
    calls.push(["fetch", String(url), new Headers(init?.headers).get("Authorization")]);
    return new Response("private artifact", { status: 200 });
  };
  globalThis.document = { createElement: (tag) => { assert.equal(tag, "a"); return link; } };
  URL.createObjectURL = () => "blob:artifact-1";
  URL.revokeObjectURL = (url) => calls.push(["revoke", url]);
  globalThis.setTimeout = (callback) => { callback(); return 0; };
  try {
    await loaded.exports.apiDownload("/v1/runs/run-1/artifacts/artifact-1", "metrics.json");
  } finally {
    globalThis.fetch = original.fetch;
    if (original.document === undefined) delete globalThis.document;
    else globalThis.document = original.document;
    URL.createObjectURL = original.createObjectURL;
    URL.revokeObjectURL = original.revokeObjectURL;
    globalThis.setTimeout = original.setTimeout;
  }
  assert.deepEqual(calls, [
    ["fetch", "http://localhost:8000/v1/runs/run-1/artifacts/artifact-1", "Bearer user-a"],
    ["click", "blob:artifact-1", "metrics.json"],
    ["revoke", "blob:artifact-1"],
  ]);
});

test("apiDownload maps denial responses to ApiError and never triggers a download", async () => {
  const original = { fetch: globalThis.fetch, document: globalThis.document };
  let createdLinks = 0;
  globalThis.fetch = async () => new Response(
    JSON.stringify({ detail: { code: "RUN_NOT_FOUND", message: "Run not found" } }),
    { status: 404, statusText: "Not Found", headers: { "Content-Type": "application/json" } },
  );
  globalThis.document = { createElement: () => { createdLinks += 1; return { click: () => {} }; } };
  try {
    await assert.rejects(
      () => loaded.exports.apiDownload("/v1/runs/run-1/artifacts/artifact-1"),
      (err) => {
        assert.equal(err.name, "ApiError");
        assert.equal(err.status, 404);
        assert.equal(err.code, "RUN_NOT_FOUND");
        return true;
      },
    );
  } finally {
    globalThis.fetch = original.fetch;
    if (original.document === undefined) delete globalThis.document;
    else globalThis.document = original.document;
  }
  assert.equal(createdLinks, 0);
});
