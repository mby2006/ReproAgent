const SUMMARY_ITEMS = [
  "dataset",
  "model architecture",
  "optimizer",
  "learning rate",
  "loss function",
  "batch size",
  "epochs",
  "target metric",
];

const SUMMARY_KEYS = {
  dataset: "dataset",
  "model architecture": "architecture",
  optimizer: "optimizer",
  "learning rate": "learning_rate",
  "loss function": "loss_function",
  "batch size": "batch_size",
  epochs: "epochs",
  "target metric": "target_metric",
};

const ITEM_ALIASES = {
  dataset: "dataset",
  dataset_name: "dataset",
  architecture: "model architecture",
  model_architecture: "model architecture",
  optimizer: "optimizer",
  learning_rate: "learning rate",
  lr: "learning rate",
  loss_function: "loss function",
  loss: "loss function",
  batch_size: "batch size",
  epochs: "epochs",
  epoch: "epochs",
  training_epochs: "epochs",
  target_metric: "target metric",
  evaluation_metric: "target metric",
};

const MISSING = "无可用证据";

function normalizeItem(value) {
  if (typeof value !== "string") return null;
  const normalized = value.trim().toLowerCase().replace(/[\s-]+/g, "_");
  return ITEM_ALIASES[normalized] ?? (SUMMARY_ITEMS.includes(normalized) ? normalized : null);
}

function displayValue(value) {
  return value == null || value === "" ? MISSING : value;
}

export function selectConditionSummary(rows) {
  const byItem = new Map();
  for (const row of rows) {
    const item = normalizeItem(row.item ?? row.key ?? row.name);
    if (item) byItem.set(item, row);
  }
  return SUMMARY_ITEMS.map((item) => {
    const row = byItem.get(item);
    const paperEvidence = row?.paper_evidence ?? null;
    return {
      item,
      key: typeof row?.key === "string" && row.key ? row.key : SUMMARY_KEYS[item],
      label: typeof row?.label === "string" && row.label ? row.label : item,
      paper: displayValue(row?.paper ?? row?.paper_value),
      plan: displayValue(row?.plan ?? row?.plan_value),
      actual: displayValue(row?.actual ?? row?.actual_value ?? row?.implementation_value),
      source: displayValue(row?.source ?? paperEvidence?.source_page ?? paperEvidence?.source_section),
      note: displayValue(row?.note),
      paper_evidence: paperEvidence,
      actual_evidence: row?.actual_evidence ?? row?.implementation_evidence ?? null,
      decision: row?.decision ?? null,
    };
  });
}
