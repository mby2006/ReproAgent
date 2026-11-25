import { existsSync, readFileSync, statSync } from "node:fs";
import { resolve } from "node:path";
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
}
