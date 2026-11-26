import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { tmpdir } from "node:os";
import { join, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { ARCHIVE_FILES, validateArtifact, verifyArchive } from "../../scripts/verify-artifact.mjs";

const webRoot = fileURLToPath(new URL("../../", import.meta.url));

function temporaryDirectory(t) {
  const directory = mkdtempSync(join(tmpdir(), "reproagent-artifact-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  return directory;
}

function writeFile(directory, path, contents = "fixture") {
  const target = join(directory, path);
  mkdirSync(join(target, ".."), { recursive: true });
  writeFileSync(target, contents);
}

test("a static artifact is publishable only with every exported route and browser assets", (t) => {
  const directory = temporaryDirectory(t);
  writeFile(directory, "site-mode.json", JSON.stringify({ mode: "static" }));
  for (const route of [
    "index.html", "404.html", "login.html", "papers.html", "health.html",
    "experiments.html", "comparisons.html", "comparison.html",
    "examples/lenet-mnist.html",
  ]) writeFile(directory, route);
  writeFile(directory, "_next/static/chunk.js");

  assert.doesNotThrow(() => validateArtifact("static", directory));

  rmSync(join(directory, "comparisons.html"));
  assert.throws(() => validateArtifact("static", directory), /comparisons\.html/);
});

test("a live artifact is runnable only with a matching marker and build id", (t) => {
  const directory = temporaryDirectory(t);
  writeFile(directory, "site-mode.json", JSON.stringify({ mode: "live" }));
  writeFile(directory, "BUILD_ID", "build-fixture");
  assert.doesNotThrow(() => validateArtifact("live", directory));

  writeFile(directory, "site-mode.json", JSON.stringify({ mode: "static" }));
  assert.throws(() => validateArtifact("live", directory), /mode mismatch/);

  writeFile(directory, "site-mode.json", JSON.stringify({ mode: "live" }));
  rmSync(join(directory, "BUILD_ID"));
  assert.throws(() => validateArtifact("live", directory), /BUILD_ID/);
});

test("the build command rejects an unknown mode before invoking Next.js", () => {
  const script = fileURLToPath(new URL("../../scripts/build-site.mjs", import.meta.url));
  const result = spawnSync(process.execPath, [script, "preview"], { encoding: "utf8" });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /live.*static|static.*live/);
  assert.doesNotMatch(result.stdout, /Creating an optimized production build/);
});

// ---- 公共归档九文件校验 ----

async function makeStaticFixture(t) {
  const root = temporaryDirectory(t);
  const files = {};
  for (const name of ARCHIVE_FILES) {
    const contents = `fixture payload: ${name}\n`;
    writeFile(root, join("examples", "lenet", name), contents);
    files[name] = createHash("sha256").update(contents).digest("hex");
  }
  writeFile(
    root,
    join("examples", "lenet", "manifest.json"),
    JSON.stringify({ run: "01993dd36ff17102", files }),
  );
  return root;
}

test("archive verifier passes when the nine manifest files match", async (t) => {
  const root = await makeStaticFixture(t);
  const result = await verifyArchive(root);
  assert.equal(result.run, "01993dd36ff17102");
  assert.equal(result.verifiedFiles, 9);
  assert.equal(result.files.length, 9);
  for (const entry of result.files) {
    assert.match(entry.expectedSha256, /^[a-f0-9]{64}$/);
    assert.equal(entry.actualSha256, entry.expectedSha256);
  }
  assert.deepEqual(
    result.files.map((entry) => entry.path.split(sep).pop()).sort(),
    [...ARCHIVE_FILES].sort(),
  );
});

test("archive verifier rejects a missing report.md", async (t) => {
  const root = await makeStaticFixture(t);
  rmSync(join(root, "examples", "lenet", "report.md"));
  await assert.rejects(verifyArchive(root), /report\.md/);
});

test("archive verifier rejects a tampered metrics file", async (t) => {
  const root = await makeStaticFixture(t);
  writeFileSync(join(root, "examples", "lenet", "metrics.json"), "{}");
  await assert.rejects(verifyArchive(root), /metrics\.json.*sha256/i);
});

test("archive verifier rejects a manifest with a tenth entry", async (t) => {
  const root = await makeStaticFixture(t);
  const path = join(root, "examples", "lenet", "manifest.json");
  const manifest = JSON.parse(await readFile(path, "utf8"));
  manifest.files["extra.bin"] = "0".repeat(64);
  writeFileSync(path, JSON.stringify(manifest));
  await assert.rejects(verifyArchive(root), /exactly nine/);
});

test("archive verifier rejects a manifest hash that is not 64 hex chars", async (t) => {
  const root = await makeStaticFixture(t);
  const path = join(root, "examples", "lenet", "manifest.json");
  const manifest = JSON.parse(await readFile(path, "utf8"));
  manifest.files["config.yaml"] = "deadbeef";
  writeFileSync(path, JSON.stringify(manifest));
  await assert.rejects(verifyArchive(root), /config\.yaml/);
});

test("the shipped public archive passes nine-file verification", async () => {
  const result = await verifyArchive(join(webRoot, "public"));
  assert.equal(result.verifiedFiles, 9);
  assert.equal(result.run, "01993dd36ff17102");
});

// ---- 公共构建 HTML：游客独立性 ----

async function readBuiltPage(name) {
  try {
    return await readFile(join(webRoot, "out", name), "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return null;
    throw error;
  }
}

test("static papers route does not mount the authenticated workbench gate", async (t) => {
  const html = await readBuiltPage("papers.html");
  if (!html) return t.skip("out/papers.html missing — run pnpm build:static first");
  assert.doesNotMatch(html, /登录后运行自己的实验/);
  assert.match(html, /公开归档可直接浏览/);
  assert.match(html, /examples\/lenet-mnist\.html/);
});

test("static archive page is guest-verifiable without any session code", async (t) => {
  const html = await readBuiltPage("examples/lenet-mnist.html");
  if (!html) return t.skip("out/examples/lenet-mnist.html missing — run pnpm build:static first");
  assert.doesNotMatch(html, /登录后运行自己的实验/);
  assert.doesNotMatch(html, /getSession/);
  assert.match(html, /01993dd36ff17102/);
  assert.match(html, /缩放版/);
  assert.match(html, /归档 ID/);
  assert.match(html, /模型 SHA-256/);
  assert.match(html, /镜像 digest/);
  assert.match(html, /两者不是同一概念/);
});

test("static homepage CTA is scoped to the LeNet-5 x MNIST run", async (t) => {
  const html = await readBuiltPage("index.html");
  if (!html) return t.skip("out/index.html missing — run pnpm build:static first");
  assert.doesNotMatch(html, /登录后运行自己的实验/);
  assert.match(html, /运行 LeNet-5 × MNIST 复现实验/);
});
