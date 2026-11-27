import { chromium } from "playwright-core";
import { createInterface } from "node:readline/promises";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { stdin, stdout } from "node:process";
import { pathToFileURL } from "node:url";
import { createHash } from "node:crypto";
import { resolve, join } from "node:path";

export function parseArgs(argv) {
  const values = {};
  const names = new Set(["--base-url", "--pdf", "--email", "--output"]);
  for (let i = 2; i < argv.length; i += 2) {
    const flag = argv[i];
    if (!names.has(flag) || !argv[i + 1]) throw new Error(`invalid argument: ${flag}`);
    values[flag.slice(2)] = argv[i + 1];
  }
  for (const flag of names) {
    if (!values[flag.slice(2)]) throw new Error(`missing required argument ${flag}`);
  }
  return { baseUrl: values["base-url"], pdf: values.pdf, email: values.email, output: values.output };
}

export function assertConfirmedCard(card, previousVersion) {
  if (!card?.id) throw new Error("confirmed Card identity is missing");
  if (!(card.version > previousVersion)) throw new Error("confirmed Card must be a newer immutable version");
  return card;
}

export function inspectGrounding(fields) {
  const rejected = fields.filter((field) => ["citation_not_found", "located_unsupported"].includes(field.grounding_status));
  for (const field of rejected) {
    if (field.evidence_quote != null || field.source_page != null) throw new Error("rejected citation has accepted evidence");
    if (!field.candidate_quote) throw new Error("rejected citation has no candidate review context");
  }
  return {
    grounding: fields.map((field) => ({
      value: field.value,
      status: field.status,
      groundingStatus: field.grounding_status ?? null,
      sourcePage: field.source_page,
      candidateCount: field.candidate_quote ? 1 : 0,
      candidatePage: field.candidate_page ?? null,
    })),
    rejectedCount: rejected.length,
  };
}

export async function inspectDownload(download, execution, output = ".", suffix = "") {
  const filename = download.suggestedFilename();
  const match = filename.match(/^report-run-(.+)-v(\d+)\.md$/);
  const version = Number(match?.[2]);
  if (!match || match[1] !== execution.runId || !Number.isInteger(version) || version < 1) {
    throw new Error("invalid report download filename or run identity");
  }
  const target = resolve(output, `report-v${version}${suffix}.md`);
  await download.saveAs(target);
  // Read the saved bytes once so hashing and UTF-8 validation cannot disagree at
  // a stream-chunk boundary. The API Markdown hash is checked by main().
  const bytes = await readFile(target);
  const markdown = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  const rows = new Map([...markdown.matchAll(/^\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|$/gm)].map((match) => [match[1], match[2]]));
  for (const [label, expected] of [
    ["运行 ID", execution.runId],
    ["计划 ID", `${execution.planId}（v${execution.planVersion}）`],
    ["代码 hash", execution.codeHash],
    ["执行尝试", String(execution.attempt)],
  ]) {
    if (rows.get(label) !== expected) throw new Error(`downloaded markdown does not match ${label}`);
  }
  if (execution.runnerRequestId && rows.get("Runner Request ID") !== execution.runnerRequestId) {
    throw new Error("downloaded markdown does not match Runner request ID");
  }
  return { path: target, filename, version, runId: execution.runId, markdownHash: createHash("sha256").update(bytes).digest("hex") };
}

export function assertReportDownload(downloaded, reportMeta) {
  if (downloaded.version !== reportMeta.version) throw new Error("downloaded report version does not match API");
  if (typeof reportMeta.markdown !== "string") throw new Error("API report Markdown is unavailable");
  const expectedHash = createHash("sha256").update(reportMeta.markdown, "utf8").digest("hex");
  if (downloaded.markdownHash !== expectedHash) throw new Error("downloaded Markdown does not match immutable API bytes");
  return expectedHash;
}

export function assertReportStable(first, second, refreshedVersion) {
  if (first.version !== refreshedVersion || second.version !== first.version) throw new Error("report version changed after refresh");
  if (second.markdownHash !== first.markdownHash) throw new Error("report bytes changed after refresh");
  return true;
}

export function assertPdfPageLinks(links, expectedPdf) {
  if (links.length < 3) throw new Error("missing PDF page links");
  const expected = new URL(expectedPdf);
  expected.hash = "";
  for (const link of links) {
    const url = new URL(link, expected.href);
    if (url.origin !== expected.origin || url.pathname !== expected.pathname) throw new Error("PDF page links must target the same PDF");
    if (!/^#page=[1-9]\d*$/.test(url.hash)) throw new Error("PDF page link has an invalid page target");
  }
}

export async function revealArchiveFiles(page) {
  const list = page.locator('[aria-label="九个归档文件"]');
  if (!(await list.isVisible())) await page.getByText("查看九个归档文件", { exact: true }).click();
  await list.waitFor({ state: "visible" });
}

export function assertArchiveFiles(hrefs, expectedFiles) {
  const actual = hrefs.map((href) => {
    const pathname = new URL(href, "http://archive.test").pathname;
    const prefix = "/examples/lenet/";
    if (!pathname.startsWith(prefix)) throw new Error("archive file link has an unexpected path");
    return pathname.slice(prefix.length);
  }).sort();
  const expected = [...expectedFiles].sort();
  if (actual.length !== 9 || JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error("archive UI must display exactly the nine manifest files");
  }
  return actual.length;
}

export function assertPredictionSwitched(first, second) {
  for (const state of [first, second]) {
    for (const key of ["index", "prediction", "label"]) {
      if (!Number.isInteger(state[key])) throw new Error("invalid prediction state");
    }
  }
  if (first.index === second.index) throw new Error("prediction sample did not switch");
  if (first.canvasHash === second.canvasHash && first.prediction === second.prediction && first.label === second.label) {
    throw new Error("prediction canvas and answers did not switch");
  }
}

export function assertPlanRow(text, label, expected) {
  const lines = text.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (!lines.includes(label) || !lines.includes(String(expected))) throw new Error(`plan row mismatch: ${label}`);
}

export async function inspectArchiveReportDownload(download, output, expectedBytes) {
  if (!/\.md$/i.test(download.suggestedFilename())) throw new Error("archive report was not Markdown");
  const target = resolve(output, "archive-report.md");
  await download.saveAs(target);
  const bytes = await readFile(target);
  if (!bytes.equals(expectedBytes)) throw new Error("archive report bytes do not match the manifest-verified report");
  return { path: target, filename: download.suggestedFilename(), markdownHash: createHash("sha256").update(bytes).digest("hex") };
}

export async function captureScreenshot(page, path, secrets) {
  const values = [...secrets].filter(Boolean);
  await page.evaluate((secretValues) => {
    const changes = [];
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      if (secretValues.some((secret) => node.nodeValue.includes(secret))) {
        changes.push({ node, value: node.nodeValue });
        node.nodeValue = secretValues.reduce((text, secret) => text.replaceAll(secret, "[redacted]"), node.nodeValue);
      }
    }
    for (const input of document.querySelectorAll("input, textarea")) {
      if (secretValues.some((secret) => input.value.includes(secret))) {
        changes.push({ input, value: input.value });
        input.value = secretValues.reduce((text, secret) => text.replaceAll(secret, "[redacted]"), input.value);
      }
    }
    window.__liveAcceptanceRedactions = changes;
  }, values);
  try {
    await page.screenshot({ path, fullPage: true });
  } finally {
    await page.evaluate(() => {
      for (const change of window.__liveAcceptanceRedactions ?? []) {
        if (change.node) change.node.nodeValue = change.value;
        if (change.input) change.input.value = change.value;
      }
      delete window.__liveAcceptanceRedactions;
    });
  }
}

export function finalizeResult(input) {
  const assertions = input.assertions ?? [];
  const { email, password, token, cookie, ...safe } = input;
  void email; void password; void token; void cookie;
  return {
    ...safe,
    assertions: assertions.map(({ name, status, error, ...details }) => ({ name, status, ...details, ...(error ? { error } : {}) })),
    passed: assertions.length > 0 && assertions.every((item) => item.status === "passed"),
  };
}

const ARCHIVE_FILES = ["train.py.txt", "model.py.txt", "evaluate.py.txt", "config.yaml", "metrics.json", "prediction_examples.json", "eval_metrics.json", "conditions.json", "report.md"];
const TIMEOUT = Number(process.env.LIVE_ACCEPTANCE_TIMEOUT_MS || 30 * 60_000);
const POLL_MS = 2_000;

function check(assertions, name, details = {}) {
  assertions.push({ name, status: "passed", ...details });
  console.error(`[passed] ${name}`);
}

function progress(message) {
  console.error(`[live-acceptance] ${message}`);
}

function safeError(error, secrets) {
  let message = String(error?.message ?? error);
  for (const secret of secrets) if (secret) message = message.replaceAll(secret, "[redacted]");
  return message.replace(/Bearer\s+[A-Za-z0-9._~+/-]+/gi, "Bearer [redacted]");
}

async function waitFor(page, predicate, description, timeout = TIMEOUT) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const value = await predicate();
    if (value) return value;
    await page.waitForTimeout(POLL_MS);
  }
  throw new Error(`timed out waiting for ${description}`);
}

async function api(page, path, baseUrl) {
  return page.evaluate(async ({ path, baseUrl }) => {
    const key = Object.keys(localStorage).find((item) => item.endsWith("-auth-token"));
    const token = key ? JSON.parse(localStorage.getItem(key)).access_token : null;
    if (!token) throw new Error("missing browser session");
    const response = await fetch(`${baseUrl}${path}`, { headers: { Authorization: `Bearer ${token}` } });
    if (!response.ok) throw new Error(`API ${path}: HTTP ${response.status}`);
    return response.json();
  }, { path, baseUrl });
}

export async function fetchReport(page, runId, baseUrl) {
  return page.evaluate(async ({ runId, baseUrl }) => {
    const key = Object.keys(localStorage).find((item) => item.endsWith("-auth-token"));
    const token = key ? JSON.parse(localStorage.getItem(key)).access_token : null;
    if (!token) throw new Error("missing browser session");
    const response = await fetch(`${baseUrl}/v1/runs/${runId}/report`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const body = await response.json();
    const code = body?.code ?? body?.detail?.code;
    if (response.status === 404 && code === "REPORT_UNAVAILABLE") return null;
    if (!response.ok) throw new Error(`API report: HTTP ${response.status}${code ? ` (${code})` : ""}`);
    return body;
  }, { runId, baseUrl });
}

export async function selectedRunId(page) {
  const runId = await page.getByLabel("选择运行").inputValue();
  if (!runId) throw new Error("no run selected");
  return runId;
}

export async function ensureRunSelected(page, runId) {
  const select = page.getByLabel("选择运行");
  await select.selectOption(runId);
  if (await select.inputValue() !== runId) throw new Error("run selection did not persist");
}

async function downloadReport(page, output, execution, suffix = "") {
  const downloading = page.waitForEvent("download", { timeout: 30_000 });
  await page.getByRole("button", { name: "下载报告", exact: true }).click();
  return inspectDownload(await downloading, execution, output, suffix);
}

async function predictionState(page) {
  return page.evaluate(() => {
    const root = document.querySelector("#try");
    const canvas = root?.querySelector("canvas");
    const ctx = canvas?.getContext("2d");
    if (!root || !ctx) throw new Error("prediction reader is not hydrated");
    const pixels = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
    let hash = 2166136261;
    for (let i = 0; i < pixels.length; i += 4) {
      hash = Math.imul(hash ^ pixels[i], 16777619);
      hash = Math.imul(hash ^ pixels[i + 1], 16777619);
      hash = Math.imul(hash ^ pixels[i + 2], 16777619);
    }
    const text = root.innerText;
    return {
      index: Number(text.match(/测试样本\s+(\d+)/)?.[1]),
      canvasHash: (hash >>> 0).toString(16).padStart(8, "0"),
      prediction: Number(text.match(/模型猜\s*(\d+)/)?.[1]),
      label: Number(text.match(/数据集标注\s*(\d+)/)?.[1]),
    };
  });
}

export async function main() {
  const args = parseArgs(process.argv);
  await mkdir(args.output, { recursive: true });
  const apiUrl = (process.env.LIVE_ACCEPTANCE_API_URL || new URL(args.baseUrl).origin.replace(/:\d+$/, ":8000")).replace(/\/$/, "");
  const evidence = { archive: {}, upload: {}, specReview: {}, planApproval: {}, execution: {}, reportDownload: {}, assertions: [] };
  const secrets = new Set([args.email]);
  let browser;
  let failure;
  try {
    progress("launching installed browser");
    browser = await chromium.launch({ headless: true, channel: process.env.BROWSER_CHANNEL || "msedge", executablePath: process.env.BROWSER_EXECUTABLE || undefined });
    const context = await browser.newContext({ acceptDownloads: true });
    const page = await context.newPage();
    page.setDefaultTimeout(60_000);
    page.setDefaultNavigationTimeout(120_000);

    // This context has never logged in: prove the archive works without auth APIs.
    await context.route("**/*", (route) => {
      const url = route.request().url();
      if (/\/v1\/|supabase|auth\//.test(url)) return route.abort();
      return route.continue();
    });
    progress("opening public archive without authentication");
    await page.goto(`${args.baseUrl}/examples/lenet-mnist`, { waitUntil: "domcontentloaded" });
    await page.getByRole("link", { name: "登录后运行自己的实验" }).waitFor();
    check(evidence.assertions, "public archive available without login");
    const manifestResponse = await page.request.get(`${args.baseUrl}/examples/lenet/manifest.json`);
    if (!manifestResponse.ok()) throw new Error("public manifest unavailable");
    const manifest = await manifestResponse.json();
    const files = Object.keys(manifest.files).sort();
    if (JSON.stringify(files) !== JSON.stringify([...ARCHIVE_FILES].sort())) throw new Error("archive must contain exactly nine manifest files");
    const archiveBytes = new Map();
    for (const file of files) {
      const response = await page.request.get(`${args.baseUrl}/examples/lenet/${file}`);
      if (!response.ok()) throw new Error(`archive file unavailable: ${file}`);
      const bytes = await response.body();
      const actual = createHash("sha256").update(bytes).digest("hex");
      if (actual !== manifest.files[file]) throw new Error(`archive hash mismatch: ${file}`);
      archiveBytes.set(file, bytes);
    }
    if (files.length !== 9) throw new Error("archive file count is not nine");
    await revealArchiveFiles(page);
    const displayedArchiveHrefs = await page.locator('[aria-label="九个归档文件"]').getByRole("link").evaluateAll((links) => links.map((link) => link.href));
    const displayedArchiveFiles = assertArchiveFiles(displayedArchiveHrefs, files);
    const pageLinkHrefs = await page.locator('a[href*="#page="]').evaluateAll((links) => links.map((link) => link.href));
    assertPdfPageLinks(pageLinkHrefs, pageLinkHrefs[0]?.split("#")[0]);
    progress("checking prediction switching and archive report download");
    progress("waiting for public prediction reader hydration");
    await page.waitForFunction(() => {
      const canvas = document.querySelector("#try canvas");
      return canvas?.getContext("2d")?.getImageData(0, 0, 1, 1).data[3] === 255;
    });
    await page.getByRole("button", { name: "直接看答案" }).click();
    await page.getByText("数据集标注", { exact: false }).waitFor();
    const firstPrediction = await predictionState(page);
    await page.getByRole("button", { name: /试试下一张|再看第一张/ }).click();
    await page.getByText("测试样本 2 /", { exact: false }).waitFor();
    await page.getByRole("button", { name: "直接看答案" }).click();
    await page.getByText("数据集标注", { exact: false }).waitFor();
    const secondPrediction = await predictionState(page);
    assertPredictionSwitched(firstPrediction, secondPrediction);
    const archiveDownloading = page.waitForEvent("download", { timeout: 30_000 });
    await page.getByRole("link", { name: "下载实验报告" }).click();
    const archiveReport = await inspectArchiveReportDownload(await archiveDownloading, args.output, archiveBytes.get("report.md"));
    evidence.archive = {
      archiveVerifiedFiles: 9,
      displayedArchiveFiles,
      pageLinks: pageLinkHrefs.length,
      pageLinkTargetsValidated: true,
      predictionSwitched: true,
      predictionSamples: [firstPrediction, secondPrediction],
      reportDownloaded: true,
      reportMarkdownHash: archiveReport.markdownHash,
      runId: manifest.run,
    };
    check(evidence.assertions, "archive nine files, page links, predictions and report", { runId: manifest.run, archiveVerifiedFiles: 9 });
    await captureScreenshot(page, join(args.output, "archive.png"), secrets);
    await context.unroute("**/*");

    progress("logging in with dedicated acceptance account");
    await page.goto(`${args.baseUrl}/login`, { waitUntil: "domcontentloaded" });
    await page.getByLabel("邮箱").fill(args.email);
    let password = process.env.LIVE_ACCEPTANCE_PASSWORD;
    if (password) secrets.add(password);
    delete process.env.LIVE_ACCEPTANCE_PASSWORD;
    if (!password) {
      const rl = createInterface({ input: stdin, output: stdout });
      try { password = (await rl.question("Live acceptance password: ")).trim(); } finally { rl.close(); }
      if (password) secrets.add(password);
    }
    if (!password) throw new Error("empty password");
    await page.getByLabel("密码").fill(password);
    password = undefined;
    await page.getByRole("button", { name: "登录", exact: true }).last().click();
    await page.waitForURL((url) => !url.pathname.startsWith("/login"));
    check(evidence.assertions, "authenticated with dedicated account");

    progress("uploading real LeNet PDF");
    await page.goto(`${args.baseUrl}/papers`, { waitUntil: "domcontentloaded" });
    await page.locator('input[type="file"]').setInputFiles(args.pdf);
    await page.getByRole("button", { name: "上传并解析" }).click();
    await page.waitForURL(/\/papers\?id=/, { timeout: 120_000 });
    const uploadUrl = new URL(page.url());
    const paperId = uploadUrl.searchParams.get("id");
    const workflowId = uploadUrl.searchParams.get("workflow");
    if (!paperId || !workflowId) throw new Error("upload did not return paper/workflow IDs");
    evidence.upload = { paperId, workflowId, status: "uploaded" };
    check(evidence.assertions, "real PDF uploaded", { paperId, workflowId });
    progress("waiting for Qwen parse, extraction and grounding");
    const workflow = await waitFor(page, async () => {
      const wf = await api(page, `/v1/workflows/${workflowId}`, apiUrl);
      if (wf.status === "failed") throw new Error(`workflow failed: ${JSON.stringify(wf.error_json ?? wf.error ?? "")}`);
      return wf.status === "awaiting_spec_review" ? wf : null;
    }, "spec review");
    evidence.upload.status = workflow.status;
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.getByRole("button", { name: /确认 Card 并继续|继续工作流（已保存）/ }).waitFor();
    await page.getByTitle("论文 PDF").waitFor();
    const cardVersion = Number(await page.getByLabel("复现卡版本").inputValue());
    const card = await api(page, `/v1/papers/${paperId}/card`, apiUrl);
    if (card.version !== cardVersion || !card.id) throw new Error("displayed Card version does not match the API");
    const fieldSelections = [
      { label: "方法 Method", path: "method", field: card.card.method },
      { label: "模型架构 Architecture", path: "model_architecture", field: card.card.model_architecture },
      { label: "数据集 Dataset", path: "dataset", field: card.card.dataset },
      { label: "优化器 Optimizer", path: "optimizer", field: card.card.optimizer },
      { label: "损失函数 Loss", path: "loss_function", field: card.card.loss_function },
      ...Object.entries(card.card.hyperparameters).map(([key, field]) => ({ label: key, path: `hyperparameters.${key}`, field })),
      ...Object.entries(card.card.data_preprocessing).map(([key, field]) => ({ label: key, path: `data_preprocessing.${key}`, field })),
      ...card.card.evaluation_metrics.map((field, index) => ({ label: `指标 ${index + 1}`, path: null, field })),
      ...card.card.reported_results.map((field) => ({ label: `${field.metric}${field.condition ? `（${field.condition}）` : ""}`, path: null, field })),
      ...card.card.baselines.map((field) => ({ label: field.name, path: null, field })),
    ];
    const grounding = inspectGrounding(fieldSelections.map((item) => item.field));
    if (grounding.rejectedCount === 0) throw new Error("LeNet Card did not exercise rejected grounding");
    const rejected = fieldSelections.find(({ path, field }) => path && ["citation_not_found", "located_unsupported"].includes(field.grounding_status));
    if (!rejected) throw new Error("rejected grounding has no editable review field");
    await page.locator("button.field-row").filter({ hasText: rejected.label }).first().click();
    const candidateHeading = page.getByText("候选定位（未通过语义核验，仅供人工复核）", { exact: true });
    await candidateHeading.waitFor();
    const candidateText = await candidateHeading.locator("..").innerText();
    if (!candidateText.includes(rejected.field.candidate_quote)) throw new Error("candidate quote is not visible in Card review");
    if (rejected.field.candidate_page != null && !candidateText.includes(`p.${rejected.field.candidate_page}`)) throw new Error("candidate page is not visible in Card review");
    evidence.specReview = {
      status: workflow.status,
      cardId: card.id,
      cardVersion,
      ...grounding,
      candidateContext: { path: rejected.path, label: rejected.label, candidatePage: rejected.field.candidate_page ?? null, visible: true },
    };
    check(evidence.assertions, "grounding inspected and card reviewed", { paperId, workflowId, cardVersion, rejectedCount: grounding.rejectedCount });
    await captureScreenshot(page, join(args.output, "spec-review.png"), secrets);
    progress("confirming Card and opening generated Plan");
    await page.getByRole("button", { name: /确认 Card 并继续|继续工作流（已保存）/ }).click();
    const confirmedCard = assertConfirmedCard(await waitFor(page, async () => {
      const latest = await api(page, `/v1/papers/${paperId}/card`, apiUrl);
      return latest.version > cardVersion ? latest : null;
    }, "confirmed Card version"), cardVersion);
    evidence.specReview.confirmedCardId = confirmedCard.id;
    evidence.specReview.confirmedCardVersion = confirmedCard.version;
    const experimentId = workflow.experiment_id || (await waitFor(page, async () => (await api(page, `/v1/workflows/${workflowId}`, apiUrl)).experiment_id, "experiment creation"));
    await page.goto(`${args.baseUrl}/experiments?id=${experimentId}&workflow=${workflowId}`, { waitUntil: "domcontentloaded" });
    await page.getByLabel("计划版本").waitFor();

    for (;;) {
      const version = Number(await page.getByLabel("计划版本").inputValue());
      const planData = await api(page, `/v1/experiments/${experimentId}/plans/${version}`, apiUrl);
      const pending = planData.plan.assumptions.filter((a) => a.requires_confirmation && !a.human_confirmed && (a.risk_tier ?? "critical") !== "info");
      if (pending.length === 0) break;
      await page.getByRole("button", { name: "确认此假设", exact: true }).first().click();
      await waitFor(page, async () => Number(await page.getByLabel("计划版本").inputValue()) > version, "new confirmed plan version");
    }
    const planVersion = Number(await page.getByLabel("计划版本").inputValue());
    const approved = await api(page, `/v1/experiments/${experimentId}/plans/${planVersion}`, apiUrl);
    const plan = approved.plan;
    if (plan.model_config.architecture !== "lenet-5" || plan.dataset_config.name !== "mnist") {
      throw new Error("LeNet Plan architecture or dataset changed");
    }
    await page.getByText("lenet-5", { exact: true }).first().waitFor();
    await page.getByText("mnist · input", { exact: false }).first().waitFor();
    for (const [label, value] of [
      ["批大小", plan.training_config.batch_size.value],
      ["训练轮数（epoch）", plan.training_config.epochs.value],
      ["时间预算 (秒)", plan.runtime_budget.max_seconds],
      ["执行目标（accuracy / error_rate / test_loss）", plan.evaluation_config.target_metric],
    ]) {
      const row = page.locator("button.field-row").filter({ hasText: label }).first();
      assertPlanRow(await row.innerText(), label, value);
    }
    evidence.planApproval = { experimentId, planId: approved.id, planVersion, architecture: plan.model_config.architecture, dataset: plan.dataset_config.name, targetMetric: plan.evaluation_config.target_metric, batchSize: plan.training_config.batch_size.value, epochs: plan.training_config.epochs.value, maxSeconds: plan.runtime_budget.max_seconds };
    check(evidence.assertions, "plan inspected and assumptions confirmed", { experimentId, planId: approved.id, planVersion });
    await captureScreenshot(page, join(args.output, "plan.png"), secrets);
    progress("approving Plan and waiting for Runner success");
    await page.getByRole("button", { name: "批准并执行", exact: true }).first().click();
    await page.getByRole("button", { name: "训练与结果" }).click();
    await page.getByLabel("选择运行").waitFor({ timeout: TIMEOUT });
    const runId = await page.getByLabel("选择运行").inputValue();
    const run = await waitFor(page, async () => {
      const detail = await api(page, `/v1/runs/${runId}`, apiUrl);
      if (["failed", "timeout", "cancelled"].includes(detail.status)) throw new Error(`run ${detail.status}: ${detail.error ?? ""}`);
      return detail.status === "succeeded" ? detail : null;
    }, "successful run");
    evidence.execution = { runId, workflowId, experimentId, status: run.status, attempt: (run.repair_attempt ?? 0) + 1, runnerRequestId: null, codeHash: run.code_hash, planId: run.plan_id, planVersion: run.plan_version };
    check(evidence.assertions, "real Runner execution succeeded", { runId, workflowId, attempt: evidence.execution.attempt });
    progress("waiting for immutable report generation");
    await waitFor(page, () => fetchReport(page, runId, apiUrl), "report generation");
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.getByRole("button", { name: "训练与结果" }).click();
    await page.getByLabel("选择运行").waitFor({ timeout: TIMEOUT });
    await ensureRunSelected(page, runId);
    progress("opening report and binding downloaded Markdown to final attempt");
    await page.getByText(/报告 v\d+ · 执行尝试 \d+/).waitFor({ timeout: 180_000 });
    await page.getByRole("button", { name: "Markdown 原文" }).click();
    const reportText = await page.locator("main").innerText();
    const reportMeta = await api(page, `/v1/runs/${runId}/report`, apiUrl);
    const reportId = reportMeta.report_id ?? reportMeta.id ?? reportMeta.report_json?.id ?? null;
    const reportVersion = reportMeta.version;
    const refs = reportMeta.report_json.references;
    const attempt = reportMeta.execution_attempt ?? refs.execution_attempt;
    for (const [label, actual, expected] of [
      ["report run", reportMeta.run_id, runId],
      ["reference run", refs.run_id, runId],
      ["reference workflow", refs.workflow_id, workflowId],
      ["reference paper", refs.paper_id, paperId],
      ["reference spec version id", refs.spec_version_id, evidence.specReview.confirmedCardId],
      ["reference spec version", refs.spec_version, evidence.specReview.confirmedCardVersion],
      ["reference plan", refs.plan_id, evidence.execution.planId],
      ["reference plan version", refs.plan_version, evidence.execution.planVersion],
      ["reference code hash", refs.code_hash, evidence.execution.codeHash],
      ["final attempt", attempt, evidence.execution.attempt],
    ]) {
      if (actual !== expected) throw new Error(`${label} mismatch`);
    }
    if (!reportId || !reportVersion || !refs.execution_request_id) throw new Error("report identity is incomplete");
    evidence.execution.attempt = attempt;
    evidence.execution.runnerRequestId = refs.execution_request_id;
    if (!reportText.includes(runId) || !reportText.includes(String(reportVersion))) throw new Error("live report identity mismatch");
    const first = await downloadReport(page, args.output, evidence.execution);
    assertReportDownload(first, reportMeta);
    evidence.reportDownload = { ...first, reportId, reportVersion, contentSha256: reportMeta.content_sha256, finalAttempt: attempt };
    await captureScreenshot(page, join(args.output, "report.png"), secrets);
    check(evidence.assertions, "downloaded report matches live identity", { runId, reportId, reportVersion, attempt, markdownHash: first.markdownHash });

    progress("refreshing and downloading the same report version again");
    await page.reload({ waitUntil: "domcontentloaded" });
    await page.getByRole("button", { name: "训练与结果" }).click();
    await page.getByLabel("选择运行").waitFor({ timeout: TIMEOUT });
    await ensureRunSelected(page, runId);
    await page.getByText(/报告 v\d+ · 执行尝试 \d+/).waitFor({ timeout: 180_000 });
    const refreshedVersion = Number((await page.getByText(/报告 v\d+ · 执行尝试 \d+/).innerText()).match(/报告 v(\d+)/)[1]);
    const second = await downloadReport(page, args.output, evidence.execution, "-refreshed");
    assertReportStable(first, second, refreshedVersion);
    evidence.reportDownload.refreshed = { reportVersion: refreshedVersion, ...second };
    check(evidence.assertions, "report version and bytes stable after refresh", { runId, reportVersion, markdownHash: second.markdownHash });
  } catch (error) {
    failure = new Error(safeError(error, secrets));
    evidence.assertions.push({ name: "closed loop", status: "failed", error: failure.message });
    if (browser) {
      try {
        const page = browser.contexts()[0]?.pages()[0];
        if (page) {
          await page.locator('input[type="password"]').evaluateAll((inputs) => inputs.forEach((input) => { input.value = ""; })).catch(() => {});
          await captureScreenshot(page, join(args.output, "failure.png"), secrets);
        }
      } catch { /* preserve original failure */ }
    }
  } finally {
    secrets.clear();
    await browser?.close();
  }
  const result = finalizeResult(evidence);
  await writeFile(join(args.output, "result.json"), JSON.stringify(result, null, 2) + "\n");
  if (failure) throw failure;
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((error) => { console.error(error.message); process.exitCode = 1; });
}
