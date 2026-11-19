import type {
  CheckStatus,
  EvidenceField,
  FinalStatus,
  ParamSource,
  RiskTier,
  RunStatus,
  WorkflowStatus,
} from "./types";

export type Tone = "ok" | "warn" | "bad" | "info" | "scale" | "mute" | "brand";

export const TONE_BADGE: Record<Tone, string> = {
  ok: "badge-ok",
  warn: "badge-warn",
  bad: "badge-bad",
  info: "badge-info",
  scale: "badge-scale",
  mute: "badge-mute",
  brand: "badge-brand",
};

export const TONE_DOT: Record<Tone, string> = {
  ok: "bg-ok",
  warn: "bg-warn",
  bad: "bg-bad",
  info: "bg-info",
  scale: "bg-scale",
  mute: "bg-fg-3",
  brand: "bg-brand",
};

export const TONE_TEXT: Record<Tone, string> = {
  ok: "text-ok",
  warn: "text-warn",
  bad: "text-bad",
  info: "text-info",
  scale: "text-scale",
  mute: "text-fg-3",
  brand: "text-brand",
};

export const WF_META: Record<WorkflowStatus, { label: string; tone: Tone }> = {
  uploaded: { label: "上传", tone: "mute" },
  parsing: { label: "解析", tone: "info" },
  awaiting_spec_review: { label: "确认 Card", tone: "warn" },
  planning: { label: "规划", tone: "info" },
  awaiting_plan_approval: { label: "批准计划", tone: "warn" },
  generating_code: { label: "生成代码", tone: "info" },
  validating_code: { label: "校验代码", tone: "info" },
  executing: { label: "执行", tone: "info" },
  repairing: { label: "自动修复", tone: "warn" },
  evaluating: { label: "评估", tone: "info" },
  completed: { label: "完成", tone: "ok" },
  failed: { label: "失败", tone: "bad" },
};

export const WF_BUSY: WorkflowStatus[] = [
  "uploaded",
  "parsing",
  "planning",
  "generating_code",
  "validating_code",
  "executing",
  "repairing",
  "evaluating",
];

export const RUN_META: Record<RunStatus, { label: string; tone: Tone }> = {
  queued: { label: "排队中", tone: "mute" },
  running: { label: "运行中", tone: "info" },
  succeeded: { label: "成功", tone: "ok" },
  failed: { label: "失败", tone: "bad" },
  timeout: { label: "超时", tone: "warn" },
  cancelled: { label: "已取消", tone: "mute" },
};

export const RUN_TERMINAL: RunStatus[] = ["succeeded", "failed", "timeout", "cancelled"];

export const CHECK_META: Record<CheckStatus, { label: string; tone: Tone }> = {
  matched: { label: "匹配", tone: "ok" },
  deviated: { label: "偏差", tone: "bad" },
  unknown: { label: "未知", tone: "warn" },
  not_applicable: { label: "不适用", tone: "mute" },
};

export const FINAL_META: Record<FinalStatus, { label: string; tone: Tone }> = {
  METHOD_VALIDATED: { label: "方法已验证（缩放运行）", tone: "ok" },
  SCALED_RUN_COMPLETED: { label: "缩放运行已完成", tone: "info" },
  PARTIAL_MATCH: { label: "部分匹配", tone: "warn" },
  DEVIATION_FOUND: { label: "发现偏差", tone: "bad" },
  INSUFFICIENT_EVIDENCE: { label: "证据不足", tone: "mute" },
  EXECUTION_FAILED: { label: "执行失败", tone: "bad" },
};

export const SOURCE_META: Record<ParamSource, { label: string; tone: Tone }> = {
  paper_explicit: { label: "原文", tone: "ok" },
  inferred: { label: "推断", tone: "warn" },
  human_confirmed: { label: "已确认", tone: "info" },
  mvp_scaling: { label: "MVP缩放", tone: "scale" },
};

export const ERROR_LABEL: Record<string, string> = {
  SYNTAX_ERROR: "语法错误",
  IMPORT_ERROR: "导入错误",
  SHAPE_MISMATCH: "形状不匹配",
  OUT_OF_MEMORY: "内存溢出",
  TIMEOUT: "超时",
  DATA_DOWNLOAD_ERROR: "数据下载错误",
  METRIC_ERROR: "指标错误",
  UNKNOWN_ERROR: "未知错误",
};

export function evidenceMeta(field: EvidenceField): { label: string; tone: Tone } {
  if (field.status === "missing") return { label: "缺失", tone: "mute" };
  if (field.status === "inferred") {
    return field.human_confirmed
      ? { label: "推断·已确认", tone: "info" }
      : { label: "推断", tone: "warn" };
  }
  if (field.status === "paper_described") {
    return field.human_confirmed
      ? { label: "原文描述·已确认", tone: "ok" }
      : { label: "原文描述", tone: "ok" };
  }
  return field.human_confirmed
    ? { label: "原文·已确认", tone: "ok" }
    : { label: "原文", tone: "ok" };
}

export const RISK_META: Record<RiskTier, { label: string; tone: Tone; hint: string }> = {
  critical: { label: "关键", tone: "bad", hint: "改变实验本质，需逐项决策" },
  routine: { label: "常规", tone: "warn", hint: "影响小、有安全默认，可批量采纳" },
  info: { label: "说明", tone: "mute", hint: "仅告知，不需确认" },
};

export function tierOf(a: { risk_tier?: RiskTier }): RiskTier {
  return a.risk_tier ?? "critical";
}

export function fmtDate(iso: string): string {
  return iso.slice(0, 16).replace("T", " ");
}

export function fmtNum(value: number | null | undefined, digits = 4): string {
  return value == null ? "—" : value.toFixed(digits);
}