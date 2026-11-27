import assert from "node:assert/strict";
import { test } from "node:test";
import { selectConditionSummary } from "./conditions.mjs";

test("condition summary keeps paper plan actual order and never invents missing values", () => {
  const rows = [
    { item: "optimizer", paper: "SGD", plan: "SGD", actual: "SGD" },
    { item: "dataset", paper: "MNIST", plan: "MNIST", actual: "MNIST" },
  ];
  const result = selectConditionSummary(rows);
  assert.deepEqual(result.map((row) => row.item), [
    "dataset", "model architecture", "optimizer", "learning rate",
    "loss function", "batch size", "epochs", "target metric",
  ]);
  assert.equal(result[0].paper, "MNIST");
  assert.equal(result[3].actual, "无可用证据");
});

test("condition summary maps evaluator and public keys while preserving table evidence", () => {
  const rows = [
    {
      key: "architecture",
      label: "模型结构",
      paper: "LeNet-5，含卷积和下采样",
      plan: "LeNet-5 简化实现",
      actual: "卷积、ReLU、最大池化、线性分类器",
      paper_evidence: { kind: "paper", source_page: 6, quote: "Figure 2" },
      actual_evidence: { kind: "code", file_path: "model.py" },
      decision: "现代简化结构",
      note: "论文包含可训练下采样。",
    },
    { key: "learning_rate", paper: "分五段递减", plan: "0.03", actual: "0.03", paper_evidence: { kind: "paper", source_section: "Appendix C" } },
    { key: "dataset", paper: "MNIST", plan: "MNIST", actual: "MNIST" },
  ];
  const result = selectConditionSummary(rows);
  assert.equal(result[1].item, "model architecture");
  assert.equal(result[1].key, "architecture");
  assert.equal(result[1].label, "模型结构");
  assert.equal(result[1].source, 6);
  assert.deepEqual(result[1].paper_evidence, rows[0].paper_evidence);
  assert.deepEqual(result[1].actual_evidence, rows[0].actual_evidence);
  assert.equal(result[1].decision, "现代简化结构");
  assert.equal(result[1].note, "论文包含可训练下采样。");
  assert.equal(result[3].paper, "分五段递减");
  assert.equal(result[3].actual, "0.03");
  assert.equal(result[3].source, "Appendix C");
});
