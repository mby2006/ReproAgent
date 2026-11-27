import { Fragment } from "react";
import type { NodeTrace, WorkflowStatus } from "@/lib/types";
import { TONE_BADGE, TONE_DOT, TONE_TEXT, WF_META } from "@/lib/ui";
import { IconCheck, IconRefresh } from "./icons";

const ORDER: WorkflowStatus[] = [
  "uploaded",
  "parsing",
  "awaiting_spec_review",
  "planning",
  "awaiting_plan_approval",
  "generating_code",
  "validating_code",
  "executing",
  "repairing",
  "evaluating",
  "completed",
  "failed",
  // cancelled is its own terminal outcome: not a variant of failed or completed.
  "cancelled",
];
const HIDDEN_UNLESS_ACTIVE: WorkflowStatus[] = ["failed", "cancelled", "repairing"];

// Internal graph node names are developer vocabulary; show what the step is
// actually doing and keep the raw name in the tooltip.
const NODE_LABELS: Record<string, string> = {
  ingest_paper: "读取 PDF",
  extract_paper: "解析论文结构",
  build_reproduction_spec: "抽取关键信息",
  human_review: "等待你确认信息卡",
  plan_experiment: "生成实验计划",
  human_approve_plan: "等待你批准计划",
  generate_code: "生成训练代码",
  validate_code: "检查代码可运行",
  execute_code: "在沙箱里训练",
  repair_code: "自动修复后重跑",
  evaluate_result: "对照论文数字",
  validate_evidence: "核对原文",
  classify_error: "分析运行错误",
  generate_report: "生成实验报告",
  persist_results: "保存结果",
  cancel_workflow: "停止工作流",
};

const STAGES: Record<string, WorkflowStatus> = {
  ingest_paper: "uploaded", extract_paper: "parsing", build_reproduction_spec: "parsing",
  validate_evidence: "parsing", human_review: "awaiting_spec_review", plan_experiment: "planning",
  human_approve_plan: "awaiting_plan_approval", generate_code: "generating_code",
  validate_code: "validating_code", execute_code: "executing", repair_code: "repairing",
  evaluate_result: "evaluating", generate_report: "evaluating",
};

export default function WorkflowStepper({
  status,
  traces,
  currentNode,
  error,
  onRetry,
  retrying,
}: {
  status: WorkflowStatus;
  traces: NodeTrace[];
  currentNode?: string | null;
  error?: string | null;
  onRetry?: () => void;
  retrying?: boolean;
}) {
  const visited = new Set(traces.filter(t => !t.error).map(t => STAGES[t.node_name]));
  const steps = ORDER.filter(key => !HIDDEN_UNLESS_ACTIVE.includes(key) || key === status || visited.has(key));
  return (
    <div className="card rise space-y-2.5 p-3">
      <div className="flex items-center overflow-x-auto pb-1">
        {steps.map((key, i) => {
          const meta = WF_META[key];
          const active = key === status;
          const stoppedHere = (status === "failed" || status === "cancelled") && !!currentNode && STAGES[currentNode] === key;
          const done = !active && visited.has(key) && !stoppedHere;
          return (
            <Fragment key={key}>
              {i > 0 && (
                <span
                  className={`h-px w-3 shrink-0 sm:w-5 ${done || active ? "bg-brand/45" : "bg-line"}`}
                />
              )}
              <span
                className={`flex shrink-0 items-center gap-1.5 rounded-full border px-3 py-1.5 text-[12.5px] transition ${
                  active
                    ? `${TONE_BADGE[meta.tone]} font-semibold`
                    : done
                      ? "border-line-soft bg-ink-900 text-fg-2"
                      : "border-transparent text-fg-3"
                }`}
                title={key}
              >
                {done ? (
                  <IconCheck className="h-3 w-3 text-ok" />
                ) : active ? (
                  <span className={`pulse-dot h-1.5 w-1.5 rounded-full ${TONE_DOT[meta.tone]}`} />
                ) : (
                  <span className="h-1.5 w-1.5 rounded-full border border-line" />
                )}
                {meta.label}
              </span>
            </Fragment>
          );
        })}
      </div>
      {currentNode && (
        <p className="text-[12.5px] text-fg-3">
          {status === "failed" ? "停止于：" : status === "cancelled" ? "已取消于：" : status === "completed" ? "最终步骤：" : "正在做："}<span className="text-fg-2">{NODE_LABELS[currentNode] ?? currentNode}</span>
          <span className="ml-1.5 font-mono text-[11px] text-fg-3" title={currentNode}>
            ({currentNode})
          </span>
        </p>
      )}
      {status === "failed" && onRetry && (
        <div className="flex">
          <button onClick={onRetry} disabled={retrying} className="btn btn-danger btn-sm ml-auto">
            <IconRefresh className="h-3.5 w-3.5" />
            {retrying ? "重试中…" : "从断点重试"}
          </button>
        </div>
      )}
      {status === "failed" && error && (
        <p className={`rounded-lg border border-bad/40 bg-bad/10 px-3 py-1.5 text-xs ${TONE_TEXT.bad}`}>
          {error}
        </p>
      )}
      {status === "cancelled" && (
        <p className="rounded-lg border border-line bg-ink-900 px-3 py-1.5 text-xs text-fg-2">
          工作流已被手动停止：取消是独立终态，既不是失败也没有产出成功结果。
        </p>
      )}
    </div>
  );
}