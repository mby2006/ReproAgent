import assert from "node:assert/strict";
import { test } from "node:test";
import { buildPublicEvidenceIndex, groupEvidenceItems } from "./evidence-index.mjs";

const hash = (char) => char.repeat(64);

test("public evidence index has five groups and preserves hash locators", () => {
  const grouped = groupEvidenceItems([
    { category: "paper", hash: hash("a") },
    { category: "plan", hash: hash("b") },
    { category: "code", hash: hash("c") },
    { category: "execution", hash: hash("d") },
    { category: "review_report", hash: hash("e") },
  ]);
  assert.deepEqual(Object.keys(grouped), ["paper", "plan", "code", "execution", "review_report"]);
  assert.equal(grouped.paper[0].hash.length, 64);
});

test("public archive derives all categories from nine files and marks absent evidence", () => {
  const files = Object.fromEntries([
    "train.py.txt", "model.py.txt", "evaluate.py.txt", "config.yaml", "metrics.json",
    "prediction_examples.json", "eval_metrics.json", "conditions.json", "report.md",
  ].map((name, index) => [name, hash(String(index + 1))]));
  const index = buildPublicEvidenceIndex({
    manifest: { run: "archive-1", image: "sha256:image", files },
    conditions: [{
      key: "optimizer", label: "优化器", plan: "sgd", actual: "sgd",
      paper_evidence: { source_page: 42 },
      actual_evidence: { kind: "config", file_path: "config.yaml" },
    }],
    report: "# 复现报告\n真实归档报告",
    pdfUrl: "https://example.test/paper.pdf",
  });
  assert.equal(index.run_id, "archive-1");
  const paper = index.items.find((item) => item.category === "paper");
  assert.equal(paper.locator.page, 1);
  assert.equal(paper.locator.source_url, "https://example.test/paper.pdf");
  assert.deepEqual(Object.keys(groupEvidenceItems(index.items)), ["paper", "plan", "code", "execution", "review_report"]);
  const code = index.items.filter((item) => item.category === "code");
  assert.deepEqual(code.map((item) => item.locator.file).sort(), [
    "config.yaml", "evaluate.py.txt", "model.py.txt", "train.py.txt",
  ]);
  assert.ok(code.every((item) => item.hash.length === 64));
  assert.ok(index.items.some((item) => item.target_type === "image" && item.hash === "sha256:image"));
  assert.ok(index.items.some((item) => item.target_type === "dataset" && item.unavailable_reason));
  assert.ok(index.items.some((item) => item.category === "paper" && item.unavailable_reason));
  for (const item of index.items) {
    assert.ok(item.id && item.target_id);
    assert.ok(Object.keys(item.locator).length > 0);
    assert.ok(Object.values(item.locator).every((value) => value !== ""));
    assert.ok(!JSON.stringify(item).includes("storage_path"));
  }
});

test("missing manifest entries produce explicit unavailable placeholders, not dead links", () => {
  const index = buildPublicEvidenceIndex({ manifest: { run: "empty", files: {} }, conditions: [], report: null });
  const grouped = groupEvidenceItems(index.items);
  assert.equal(grouped.code.length, 1);
  assert.equal(grouped.review_report.length, 1);
  assert.ok(grouped.code[0].unavailable_reason);
  assert.ok(grouped.review_report[0].unavailable_reason);
  assert.ok(index.items.every((item) => item.unavailable_reason || item.hash || item.target_type === "run" || item.target_type === "dataset" || item.target_type === "paper"));
});
