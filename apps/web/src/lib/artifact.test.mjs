import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { validateArtifact } from "../../scripts/verify-artifact.mjs";

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
