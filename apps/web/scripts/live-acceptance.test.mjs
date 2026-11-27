import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { parseArgs, inspectDownload, finalizeResult, selectedRunId, ensureRunSelected, captureScreenshot, assertConfirmedCard, inspectGrounding, fetchReport, assertPdfPageLinks, revealArchiveFiles, assertArchiveFiles, assertPredictionSwitched, assertReportStable, inspectArchiveReportDownload, assertPlanRow } from "./live-acceptance.mjs";
import * as liveAcceptance from "./live-acceptance.mjs";

test("reads the exact run option value from a fake page", async () => {
  const runId = "11111111-1111-4111-8111-111111111111";
  const page = { getByLabel: (name) => {
    assert.equal(name, "选择运行");
    return { inputValue: async () => runId };
  } };
  assert.equal(await selectedRunId(page), runId);
});

test("refresh reselects the exact run before opening its report", async () => {
  const runId = "11111111-1111-4111-8111-111111111111";
  let selected = "";
  const page = { getByLabel: (name) => {
    assert.equal(name, "选择运行");
    return {
      selectOption: async (value) => { selected = value; },
      inputValue: async () => selected,
    };
  } };
  await ensureRunSelected(page, runId);
  assert.equal(selected, runId);
});

test("requires all live acceptance arguments", () => {
  assert.deepEqual(parseArgs(["node", "live-acceptance.mjs", "--base-url", "http://localhost:3000", "--pdf", "paper.pdf", "--email", "acceptance@example.test", "--output", "evidence"]), {
    baseUrl: "http://localhost:3000", pdf: "paper.pdf", email: "acceptance@example.test", output: "evidence",
  });
  assert.throws(() => parseArgs(["node", "live-acceptance.mjs", "--base-url", "http://localhost:3000", "--pdf", "paper.pdf", "--email", "acceptance@example.test", "--output", "evidence", "--resume-workflow", "workflow-1"]), /invalid argument/);
  for (const missing of ["--base-url", "--pdf", "--email", "--output"]) {
    const args = ["--base-url", "http://localhost:3000", "--pdf", "paper.pdf", "--email", "acceptance@example.test", "--output", "evidence"];
    args.splice(args.indexOf(missing), 2);
    assert.throws(() => parseArgs(["node", "live-acceptance.mjs", ...args]), new RegExp(missing.slice(2)));
  }
});

test("confirmed Card identity must be a newer immutable version", () => {
  const confirmed = { id: "spec-v2", version: 2 };
  assert.equal(assertConfirmedCard(confirmed, 1), confirmed);
  assert.throws(() => assertConfirmedCard({ id: "spec-v1", version: 1 }, 1), /newer/);
  assert.throws(() => assertConfirmedCard({ id: null, version: 2 }, 1), /identity/);
});

test("download inspection binds report identity to the live run and hashes bytes", async () => {
  const output = await mkdtemp(join(tmpdir(), "live-acceptance-"));
  const execution = { runId: "11111111-1111-4111-8111-111111111111", planId: "22222222-2222-4222-8222-222222222222", planVersion: 3, codeHash: "a".repeat(64), attempt: 1, runnerRequestId: "runner-request-1" };
  const markdown = `| 运行 ID | ${execution.runId} |\n| 计划 ID | ${execution.planId}（v${execution.planVersion}） |\n| 代码 hash | ${execution.codeHash} |\n| 执行尝试 | ${execution.attempt} |\n| Runner Request ID | ${execution.runnerRequestId} |\n| 说明 | 中文边界：\u00e9 |\n`;
  const bytes = new TextEncoder().encode(markdown);
  const download = {
    suggestedFilename: () => `report-run-${execution.runId}-v2.md`,
    saveAs: async (path) => { await writeFile(path, bytes); },
  };
  try {
    const inspected = await inspectDownload(download, execution, output);
    assert.equal(inspected.runId, execution.runId);
    assert.match(inspected.markdownHash, /^[a-f0-9]{64}$/);
    assert.equal(inspected.version, 2);
    assert.match(inspected.path, /report-v2\.md$/);
    const result = finalizeResult({ execution, reportDownload: inspected, assertions: [{ name: "download", status: "passed" }] });
    assert.equal(result.reportDownload.runId, result.execution.runId);
    await assert.rejects(inspectDownload({
      ...download,
      saveAs: async (path) => { await writeFile(path, "| 运行 ID | wrong |\n"); return { path }; },
    }, execution), /运行 ID/);
    await assert.rejects(inspectDownload({
      ...download,
      saveAs: async (path) => { await writeFile(path, new Uint8Array([0xff, 0xfe])); return { path }; },
    }, execution), /decode|utf/i);
  } finally {
    await rm(output, { recursive: true, force: true });
  }
});

test("report download hash matches API Markdown bytes, not the combined content hash", () => {
  const markdown = "| 运行 ID | 11111111-1111-4111-8111-111111111111 |\n| 说明 | 中文：é |\n";
  const markdownHash = createHash("sha256").update(markdown, "utf8").digest("hex");
  const reportMeta = {
    version: 2,
    markdown,
    content_sha256: "f".repeat(64),
  };
  assert.equal(liveAcceptance.assertReportDownload({ version: 2, markdownHash }, reportMeta), markdownHash);
  assert.throws(
    () => liveAcceptance.assertReportDownload({ version: 2, markdownHash: reportMeta.content_sha256 }, reportMeta),
    /downloaded Markdown/,
  );
  assert.throws(
    () => liveAcceptance.assertReportDownload({ version: 1, markdownHash }, reportMeta),
    /version/,
  );
});

test("refreshed report must keep the downloaded version and bytes unchanged", () => {
  const first = { version: 2, markdownHash: "a".repeat(64) };
  assert.equal(assertReportStable(first, { ...first }, 2), true);
  assert.throws(() => assertReportStable(first, { ...first, version: 3 }, 2), /version/);
  assert.throws(() => assertReportStable(first, { ...first, markdownHash: "b".repeat(64) }, 2), /bytes/);
  assert.throws(() => assertReportStable(first, { ...first }, 3), /version/);
});

test("screenshots redact account secrets and restore the page afterward", async () => {
  const email = "acceptance@example.test";
  const password = "not-a-real-password";
  const textNode = { nodeValue: `Signed in as ${email}` };
  const input = { value: password };
  const originalDocument = globalThis.document;
  const originalWindow = globalThis.window;
  const originalNodeFilter = globalThis.NodeFilter;
  globalThis.document = {
    body: {},
    createTreeWalker: () => {
      let emitted = false;
      return { nextNode: () => {
        if (emitted) return null;
        emitted = true;
        return textNode;
      } };
    },
    querySelectorAll: () => [input],
  };
  globalThis.window = {};
  globalThis.NodeFilter = { SHOW_TEXT: 4 };
  let restoring = false;
  const page = {
    evaluate: async (callback, secrets) => {
      if (secrets) { callback(secrets); return; }
      restoring = true;
      callback();
    },
    screenshot: async ({ path }) => {
      assert.equal(restoring, false);
      assert.equal(path, "evidence.png");
      assert.equal(textNode.nodeValue, "Signed in as [redacted]");
      assert.equal(input.value, "[redacted]");
    },
  };
  try {
    await captureScreenshot(page, "evidence.png", new Set([email, password]));
    assert.equal(textNode.nodeValue, `Signed in as ${email}`);
    assert.equal(input.value, password);
    assert.equal(globalThis.window.__liveAcceptanceRedactions, undefined);
  } finally {
    if (originalDocument === undefined) delete globalThis.document;
    else globalThis.document = originalDocument;
    if (originalWindow === undefined) delete globalThis.window;
    else globalThis.window = originalWindow;
    if (originalNodeFilter === undefined) delete globalThis.NodeFilter;
    else globalThis.NodeFilter = originalNodeFilter;
  }
});

test("failed assertion never passes the result and secrets are excluded", () => {
  const result = finalizeResult({
    archive: { archiveVerifiedFiles: 9 }, upload: {}, specReview: {}, planApproval: {}, execution: {}, reportDownload: {},
    assertions: [{ name: "archive", status: "passed" }, { name: "run", status: "failed", error: "bad" }],
  });
  assert.equal(result.passed, false);
  assert.equal(result.assertions.every((item) => item.status === "passed"), result.passed);
  assert.doesNotMatch(JSON.stringify(result), /password|token|cookie|acceptance@example\.test/i);
});

test("rejected grounding keeps only candidate review context", () => {
  const fields = [
    { value: "LeNet-5", status: "paper_explicit", grounding_status: "verified", evidence_quote: "LeNet-5", source_page: 2 },
    { value: "MNIST", status: "inferred", grounding_status: "located_unsupported", evidence_quote: null, source_page: null, candidate_quote: "NIST Special Database 3", candidate_page: 10 },
    { value: 20, status: "inferred", grounding_status: "citation_not_found", evidence_quote: null, source_page: null, candidate_quote: "Twenty iterations", candidate_page: null },
    { value: null, status: "missing", grounding_status: "not_checked", evidence_quote: null, source_page: null },
  ];
  const result = inspectGrounding(fields);
  assert.equal(result.rejectedCount, 2);
  assert.deepEqual(result.grounding.map((field) => field.candidateCount), [0, 1, 1, 0]);
  assert.throws(() => inspectGrounding([{ ...fields[1], evidence_quote: "accepted" }]), /accepted evidence/);
  assert.throws(() => inspectGrounding([{ ...fields[1], candidate_quote: null }]), /candidate/);
});

test("archive page links target valid pages in one PDF", () => {
  const base = "https://papers.example/LeNet.pdf";
  assertPdfPageLinks([`${base}#page=1`, `${base}#page=6`, `${base}#page=42`], base);
  assert.throws(() => assertPdfPageLinks([`${base}#page=0`], base), /page/);
  assert.throws(() => assertPdfPageLinks([`${base}#page=6`, `${base}#page=10`, "https://other.example/paper.pdf#page=6"], base), /same PDF/);
});

test("archive file inspection expands a collapsed details list before reading links", async () => {
  let visible = false;
  let clicked = false;
  const list = {
    isVisible: async () => visible,
    waitFor: async ({ state }) => {
      assert.equal(state, "visible");
      assert.equal(visible, true);
    },
  };
  const page = {
    locator: (selector) => {
      assert.equal(selector, '[aria-label="九个归档文件"]');
      return list;
    },
    getByText: (text) => {
      assert.equal(text, "查看九个归档文件");
      return { click: async () => { clicked = true; visible = true; } };
    },
  };
  await revealArchiveFiles(page);
  assert.equal(clicked, true);
});

test("archive UI must display exactly the nine manifest files", () => {
  const files = ["train.py.txt", "model.py.txt", "evaluate.py.txt", "config.yaml", "metrics.json", "prediction_examples.json", "eval_metrics.json", "conditions.json", "report.md"];
  assertArchiveFiles(files.map((file) => `/examples/lenet/${file}`), files);
  assert.throws(() => assertArchiveFiles(files.slice(1).map((file) => `/examples/lenet/${file}`), files), /nine/);
  assert.throws(() => assertArchiveFiles([...files, "extra.txt"].map((file) => `/examples/lenet/${file}`), files), /nine/);
});

test("prediction switching compares the sample, canvas pixels and revealed answers", () => {
  assertPredictionSwitched(
    { index: 0, canvasHash: "a".repeat(64), prediction: 7, label: 7 },
    { index: 1, canvasHash: "b".repeat(64), prediction: 2, label: 2 },
  );
  assert.throws(() => assertPredictionSwitched(
    { index: 0, canvasHash: "a".repeat(64), prediction: 7, label: 7 },
    { index: 0, canvasHash: "a".repeat(64), prediction: 7, label: 7 },
  ), /did not switch/);
  assert.throws(() => assertPredictionSwitched(
    { index: NaN, canvasHash: "a".repeat(64), prediction: NaN, label: NaN },
    { index: NaN, canvasHash: "b".repeat(64), prediction: NaN, label: NaN },
  ), /invalid prediction state/);
});

test("archive report download must match the manifest-verified bytes", async () => {
  const output = await mkdtemp(join(tmpdir(), "archive-acceptance-"));
  const expected = new TextEncoder().encode("# archive report\n中文：é\n");
  try {
    const download = {
      suggestedFilename: () => "report.md",
      saveAs: async (path) => { await writeFile(path, expected); },
    };
    const inspected = await inspectArchiveReportDownload(download, output, expected);
    assert.equal(inspected.markdownHash, createHash("sha256").update(expected).digest("hex"));
    await assert.rejects(inspectArchiveReportDownload({
      ...download,
      saveAs: async (path) => { await writeFile(path, "wrong"); },
    }, output, expected), /archive report bytes/);
  } finally {
    await rm(output, { recursive: true, force: true });
  }
});

test("plan row assertions bind each displayed value to its own control", () => {
  assertPlanRow("批大小\n64", "批大小", 64);
  assertPlanRow("训练轮数（epoch）\n3", "训练轮数（epoch）", 3);
  assert.throws(() => assertPlanRow("批大小\n32\n训练轮数（epoch）\n3", "批大小", 64), /批大小/);
  assert.throws(() => assertPlanRow("批大小\n64", "训练轮数（epoch）", 3), /训练轮数/);
});

test("report polling treats only REPORT_UNAVAILABLE as pending", async () => {
  const originalFetch = globalThis.fetch;
  const originalLocalStorage = globalThis.localStorage;
  const runId = "11111111-1111-4111-8111-111111111111";
  globalThis.localStorage = {
    "sb-test-auth-token": JSON.stringify({ access_token: "test-token" }),
    getItem(key) { return this[key] ?? null; },
  };
  const page = { evaluate: async (callback, arg) => callback(arg) };
  try {
    globalThis.fetch = async () => ({
      status: 404,
      ok: false,
      json: async () => ({ detail: { code: "REPORT_UNAVAILABLE", message: "报告尚未生成" } }),
    });
    assert.equal(await fetchReport(page, runId, "http://api.test"), null);

    globalThis.fetch = async () => ({
      status: 500,
      ok: false,
      json: async () => ({ code: "INTERNAL" }),
    });
    await assert.rejects(fetchReport(page, runId, "http://api.test"), /HTTP 500/);

    const report = { id: "report-1", run_id: runId, version: 1 };
    globalThis.fetch = async () => ({
      status: 200,
      ok: true,
      json: async () => report,
    });
    assert.deepEqual(await fetchReport(page, runId, "http://api.test"), report);
  } finally {
    globalThis.fetch = originalFetch;
    if (originalLocalStorage === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = originalLocalStorage;
  }
});
