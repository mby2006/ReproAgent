/** @typedef {import("./types").EvidenceIndexItem} EvidenceIndexItem */
/** @typedef {import("./types").EvidenceIndex} EvidenceIndexData */

/** @type {EvidenceIndexItem["category"][]} */
export const EVIDENCE_CATEGORIES = ["paper", "plan", "code", "execution", "review_report"];

/**
 * Stable five-section ordering for both the private index and static archive.
 * @param {EvidenceIndexItem[]} [items]
 * @returns {Record<import("./types").EvidenceCategory, EvidenceIndexItem[]>}
 */
export function groupEvidenceItems(items = []) {
  const grouped = Object.fromEntries(EVIDENCE_CATEGORIES.map((category) => [category, []]));
  for (const item of items) {
    if (Object.hasOwn(grouped, item.category)) grouped[item.category].push(item);
  }
  return grouped;
}

/**
 * @param {EvidenceIndexItem["category"]} category
 * @param {string} targetType
 * @param {string | number} targetId
 * @param {Record<string, string | number>} locator
 * @param {string | null} [hash]
 * @param {string | null} [unavailableReason]
 * @returns {EvidenceIndexItem}
 */
function item(category, targetType, targetId, locator, hash = null, unavailableReason = null) {
  return {
    id: `${category}:${targetType}:${targetId}`,
    category,
    target_type: targetType,
    target_id: String(targetId),
    locator,
    hash,
    unavailable_reason: unavailableReason,
  };
}

/**
 * Derive evidence solely from the checked-in nine-file archive. No network or
 * Supabase access belongs here; absent archive facts stay explicitly absent.
 * @param {{
 *   manifest?: { run?: string; files?: Record<string, string>; image?: string | null },
 *   conditions?: Array<{ key?: string; actual?: unknown }>,
 *   report?: { file?: string } | null,
 *   pdfUrl?: string | null,
 * }} options
 * @returns {EvidenceIndexData}
 */
export function buildPublicEvidenceIndex({ manifest, conditions = [], report, pdfUrl = null }) {
  const run = manifest?.run ?? "archive";
  const files = manifest?.files ?? {};
  const items = [];
  const paperLocator = { file: "report.md", source: "archive" };
  const sourcePage = conditions
    .map((row) => row?.paper_evidence?.source_page)
    .find((page) => Number.isInteger(page) && page > 0);
  if (sourcePage !== undefined) paperLocator.page = sourcePage;
  if (pdfUrl) paperLocator.source_url = pdfUrl;
  items.push(item("paper", "paper", run, paperLocator, null,
    "归档未包含论文 PDF，无法核验论文 SHA-256"));
  items.push(item("plan", "plan", run, { file: "conditions.json", source: "archive" },
    files["conditions.json"] ?? null,
    files["conditions.json"] ? null : "归档缺少 conditions.json，计划条件不可用"));

  const codeFiles = ["train.py.txt", "model.py.txt", "evaluate.py.txt", "config.yaml"];
  const presentCode = codeFiles.filter((file) => files[file]);
  for (const file of presentCode) {
    items.push(item("code", "generated_file", file, { file, run_id: run }, files[file]));
  }
  if (!presentCode.length) {
    items.push(item("code", "generated_file", run, { file: "manifest.json", run_id: run }, null,
      "九个归档文件中没有可用代码或配置"));
  }

  items.push(item("execution", "run", run, { run_id: run, source: "archive" }));
  for (const file of ["metrics.json", "eval_metrics.json", "prediction_examples.json"]) {
    if (files[file]) items.push(item("execution", "artifact", file, { file, run_id: run }, files[file]));
  }
  if (!files["metrics.json"] && !files["eval_metrics.json"]) {
    items.push(item("execution", "metrics", run, { file: "manifest.json", run_id: run }, null,
      "归档缺少最终指标文件"));
  }
  items.push(item("execution", "image", run, { run_id: run, audit_field: "image" },
    manifest?.image ?? null, manifest?.image ? null : "归档未记录执行镜像 digest"));
  const dataset = conditions.find((row) => row.key === "dataset_fingerprint")?.actual;
  items.push(item("execution", "dataset", run, { file: "conditions.json", run_id: run },
    typeof dataset === "string" && dataset ? dataset : null,
    typeof dataset === "string" && dataset ? null : "归档未记录数据集 fingerprint"));

  if (report && files["report.md"]) {
    items.push(item("review_report", "report", "report.md", { file: "report.md", run_id: run },
      files["report.md"]));
  } else {
    items.push(item("review_report", "report", run, { file: "report.md", run_id: run }, null,
      "归档缺少 report.md 或其 manifest 指纹"));
  }
  return { run_id: run, items };
}
