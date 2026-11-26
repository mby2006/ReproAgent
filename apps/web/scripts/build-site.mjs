import { spawnSync } from "node:child_process";
import { rmSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { validateArtifact, verifyArchive } from "./verify-artifact.mjs";

const mode = process.argv[2];
if (mode !== "live" && mode !== "static") {
  console.error("Usage: node scripts/build-site.mjs <live|static>");
  process.exit(1);
}

const webRoot = fileURLToPath(new URL("../", import.meta.url));
const nextDirectory = resolve(webRoot, ".next");
rmSync(resolve(nextDirectory, "site-mode.json"), { force: true });
rmSync(resolve(webRoot, "out/site-mode.json"), { force: true });

const environment = { ...process.env, NEXT_PUBLIC_SITE_MODE: mode };
delete environment.SITE_STATIC_EXPORT;
const require = createRequire(import.meta.url);
const result = spawnSync(process.execPath, [require.resolve("next/dist/bin/next"), "build"], {
  cwd: webRoot,
  env: environment,
  stdio: "inherit",
});
if (result.error) throw result.error;
if (result.status !== 0) process.exit(result.status ?? 1);

writeFileSync(resolve(nextDirectory, "site-mode.json"), `${JSON.stringify({ mode })}\n`);
if (mode === "static") {
  writeFileSync(resolve(webRoot, "out/site-mode.json"), `${JSON.stringify({ mode })}\n`);
}
validateArtifact(mode, mode === "static" ? resolve(webRoot, "out") : nextDirectory);
if (mode === "static") {
  const archive = await verifyArchive(resolve(webRoot, "out"));
  console.log(`Archive run ${archive.run}: verifiedFiles: ${archive.verifiedFiles}`);
}
console.log(`Built and verified ${mode} artifact`);
