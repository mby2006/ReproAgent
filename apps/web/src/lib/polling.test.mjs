// Run with node --test; no browser, network, database, or added dependencies.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import ts from "typescript";

const source = readFileSync(new URL("./polling.ts", import.meta.url), "utf8");
const { outputText } = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
});
const loaded = { exports: {} };
new Function("require", "module", "exports", outputText)(
  (name) => { throw new Error(`unexpected import: ${name}`); }, loaded, loaded.exports,
);
const { readOnce, startVisiblePolling } = loaded.exports;

class FakeDocument {
  hidden = true;
  listeners = new Map();
  addEventListener(name, callback) { this.listeners.set(name, callback); }
  removeEventListener(name, callback) {
    if (this.listeners.get(name) === callback) this.listeners.delete(name);
  }
  visible() {
    this.hidden = false;
    this.listeners.get("visibilitychange")?.();
  }
  hide() {
    this.hidden = true;
    this.listeners.get("visibilitychange")?.();
  }
}

class FakeScheduler {
  nextId = 1;
  callbacks = new Map();
  setInterval = (callback) => {
    const id = this.nextId++;
    this.callbacks.set(id, callback);
    return id;
  };
  clearInterval = (id) => { this.callbacks.delete(id); };
  tick() { for (const callback of [...this.callbacks.values()]) callback(); }
}

function environment() {
  const document = new FakeDocument();
  const scheduler = new FakeScheduler();
  return { document, scheduler };
}

const flush = () => new Promise((resolve) => setImmediate(resolve));

test("hidden documents schedule no polling and visibility starts an immediate refresh", async () => {
  const env = environment();
  let calls = 0;
  const stop = startVisiblePolling(() => { calls++; }, 2000, env);
  assert.equal(calls, 0);
  assert.equal(env.scheduler.callbacks.size, 0);
  env.document.visible();
  await flush();
  assert.equal(calls, 1);
  assert.equal(env.scheduler.callbacks.size, 1);
  env.scheduler.tick();
  await flush();
  assert.equal(calls, 2);
  stop();
});

test("hiding clears timers and returning visible refreshes without waiting for the interval", async () => {
  const env = environment();
  env.document.hidden = false;
  let calls = 0;
  const stop = startVisiblePolling(() => { calls++; }, 2000, env);
  await flush();
  assert.equal(calls, 1);
  env.document.hide();
  assert.equal(env.scheduler.callbacks.size, 0);
  env.scheduler.tick();
  await flush();
  assert.equal(calls, 1);
  env.document.visible();
  await flush();
  assert.equal(calls, 2);
  stop();
  assert.equal(env.scheduler.callbacks.size, 0);
});

test("a slow poll cannot overlap itself and the next interval can run after it settles", async () => {
  const env = environment();
  env.document.hidden = false;
  let resolve;
  let calls = 0;
  const stop = startVisiblePolling(() => {
    calls++;
    return new Promise((done) => { resolve = done; });
  }, 2000, env);
  await flush();
  env.scheduler.tick();
  env.scheduler.tick();
  await flush();
  assert.equal(calls, 1);
  resolve();
  await flush();
  env.scheduler.tick();
  await flush();
  assert.equal(calls, 2);
  resolve();
  await flush();
  stop();
});

test("readOnce skips a cross-tab request when another tab holds the lock", async () => {
  let calls = 0;
  const locks = {
    async request(key, options, callback) {
      assert.equal(key, "reproagent-read:/v1/runs/1");
      assert.deepEqual(options, { ifAvailable: true });
      return callback(null);
    },
  };
  const result = await readOnce("reproagent-read:/v1/runs/1", async () => { calls++; return {}; }, locks);
  assert.equal(result, null);
  assert.equal(calls, 0);
});

test("readOnce executes with the lock and coalesces overlapping same-tab reads", async () => {
  let calls = 0;
  let release;
  const locks = {
    async request(key, options, callback) { return callback({ name: key }); },
  };
  const request = () => {
    calls++;
    return new Promise((resolve) => { release = resolve; });
  };
  const first = readOnce("reproagent-read:/v1/runs/1", request, locks);
  const second = readOnce("reproagent-read:/v1/runs/1", request, locks);
  release("detail");
  assert.deepEqual(await Promise.all([first, second]), ["detail", "detail"]);
  assert.equal(calls, 1);
});

test("failed same-tab reads are removed so a later poll can retry", async () => {
  let calls = 0;
  const request = async () => {
    calls++;
    if (calls === 1) throw new Error("offline");
    return "recovered";
  };
  await assert.rejects(readOnce("reproagent-read:/v1/workflows/1", request, null), /offline/);
  assert.equal(await readOnce("reproagent-read:/v1/workflows/1", request, null), "recovered");
  assert.equal(calls, 2);
});
