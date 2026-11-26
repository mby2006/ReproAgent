import { existsSync, readFileSync, statSync } from "node:fs";
import { readFile, stat } from "node:fs/promises";
import { createHash } from "node:crypto";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const STATIC_ROUTES = [
  "index.html",
  "404.html",
  "login.html",
  "papers.html",
  "health.html",
  "experiments.html",
  "comparisons.html",
  "comparison.html",
  "examples/lenet-mnist.html",
];

// 公共归档的九个文件：manifest.files 必须与此清单逐项一致，不多不少。
export const ARCHIVE_FILES = [
  "train.py.txt",
  "model.py.txt",
  "evaluate.py.txt",
  "config.yaml",
  "metrics.json",
  "prediction_examples.json",
  "eval_metrics.json",
  "conditions.json",
  "report.md",
];

export async function verifyArchive(staticRoot, archiveRelativeDir = "/examples/lenet") {
  const dir = join(staticRoot, archiveRelativeDir);
  const manifestPath = join(dir, "manifest.json");
  let manifest;
  try {
    manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  } catch (error) {
    throw new Error(`archive manifest unreadable: ${manifestPath} (${error.message})`);
  }
  const names = Object.keys(manifest.files ?? {}).sort();
  if (JSON.stringify(names) !== JSON.stringify([...ARCHIVE_FILES].sort())) {
    throw new Error(`archive manifest must contain exactly nine files: ${names.join(", ")}`);
  }
  const files = [];
  for (const name of ARCHIVE_FILES) {
    const expectedSha256 = manifest.files[name];
    if (typeof expectedSha256 !== "string" || !/^[a-f0-9]{64}$/.test(expectedSha256)) {
      throw new Error(`invalid manifest sha256 for ${name}`);
    }
    const path = join(dir, name);
    let stats;
    try {
      stats = await stat(path);
    } catch {
      throw new Error(`archive file ${name} is missing: ${path}`);
    }
    if (!stats.isFile()) throw new Error(`archive file ${name} is not a regular file: ${path}`);
    const actualSha256 = createHash("sha256").update(await readFile(path)).digest("hex");
    if (actualSha256 !== expectedSha256) {
      throw new Error(
        `archive file ${name}: sha256 mismatch (expected ${expectedSha256}, actual ${actualSha256})`,
      );
    }
    files.push({ path, expectedSha256, actualSha256 });
  }
  return { run: manifest.run, verifiedFiles: ARCHIVE_FILES.length, files };
}

export function validateArtifact(mode, directory) {
  if (mode !== "live" && mode !== "static") {
    throw new Error("Artifact mode must be either live or static");
  }

  const markerPath = resolve(directory, "site-mode.json");
  if (!existsSync(markerPath)) throw new Error(`Missing artifact marker: ${markerPath}`);
  const marker = JSON.parse(readFileSync(markerPath, "utf8"));
  if (marker.mode !== mode) {
    throw new Error(`Artifact mode mismatch: expected ${mode}, found ${marker.mode}`);
  }

  const required = mode === "static" ? [...STATIC_ROUTES, "_next/static"] : ["BUILD_ID"];
  for (const relativePath of required) {
    const target = resolve(directory, relativePath);
    if (!existsSync(target)) throw new Error(`Missing ${mode} artifact entry: ${relativePath}`);
    if (relativePath === "_next/static" && !statSync(target).isDirectory()) {
      throw new Error(`Static asset entry is not a directory: ${relativePath}`);
    }
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const mode = process.argv[2];
  const webRoot = fileURLToPath(new URL("../", import.meta.url));
  const directory = resolve(webRoot, mode === "static" ? "out" : ".next");
  validateArtifact(mode, directory);
  console.log(`Verified ${mode} artifact: ${directory}`);
  if (mode === "static") {
    const archive = await verifyArchive(directory);
    console.log(`Archive run ${archive.run}: verifiedFiles: ${archive.verifiedFiles}`);
  }
}
