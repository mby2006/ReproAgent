import type { EvidenceField } from "@/lib/types";

export function FieldEvidence({ field }: { field: EvidenceField }) {
  if (field.grounding_status === "citation_not_found" || field.grounding_status === "located_unsupported") {
    return <div className="space-y-2 rounded-lg border border-warn/30 bg-warn/5 p-3">
      <p className="text-xs font-semibold text-warn">候选定位（未通过语义核验，仅供人工复核）</p>
      {field.candidate_quote && <blockquote className="quote">“{field.candidate_quote}”</blockquote>}
      <p className="font-mono text-[10.5px] text-fg-3">候选页：{field.candidate_page == null ? "未定位" : `p.${field.candidate_page}`}</p>
    </div>;
  }
  if (field.evidence_quote) return <blockquote className="quote">“{field.evidence_quote}”</blockquote>;
  return <p className="text-sm text-fg-3">
    {field.status === "missing" ? "论文中未找到该信息。" : "无原文引文（推断值，请人工确认）。"}
  </p>;
}
