import type { WorkflowStatus } from "@/lib/types";

const STEPS: { key: WorkflowStatus; label: string }[] = [
  { key: "uploaded", label: "上传" },
  { key: "parsing", label: "解析" },
  { key: "awaiting_spec_review", label: "确认 Card" },
  { key: "planning", label: "规划" },
  { key: "awaiting_plan_approval", label: "批准计划" },
  { key: "generating_code", label: "生成代码" },
  { key: "validating_code", label: "校验代码" },
  { key: "executing", label: "执行" },
  { key: "repairing", label: "自动修复" },
  { key: "evaluating", label: "评估" },
  { key: "completed", label: "完成" },
  { key: "failed", label: "失败" },
];

export default function WorkflowStepper({
  status,
  currentNode,
  error,
  onRetry,
  retrying,
}: {
  status: WorkflowStatus;
  currentNode?: string | null;
  error?: string | null;
  onRetry?: () => void;
  retrying?: boolean;
}) {
  return (
    <div className="space-y-2 rounded-xl border border-zinc-800 bg-zinc-900 p-3">
      <div className="flex flex-wrap items-center gap-1.5">
        {STEPS.map((step) => {
          const active = step.key === status;
          const isFail = step.key === "failed";
          const isDone = step.key === "completed";
          const cls = active
            ? isFail
              ? "border-red-500 bg-red-900 text-red-100"
              : isDone
                ? "border-emerald-500 bg-emerald-800 text-emerald-100"
                : "border-emerald-500 bg-zinc-800 text-emerald-300"
            : "border-zinc-800 bg-zinc-950 text-zinc-500";
          return (
            <span
              key={step.key}
              className={`rounded-full border px-2.5 py-0.5 text-xs ${cls} ${
                active ? "font-semibold" : ""
              }`}
            >
              {step.label}
            </span>
          );
        })}
      </div>
      <p className="text-xs text-zinc-500">
        当前节点：<span className="text-zinc-300">{currentNode ?? "—"}</span>
      </p>
      {status === "failed" && error && (
        <p className="rounded border border-red-800 bg-red-950 px-3 py-1.5 text-xs text-red-300">
          {error}
        </p>
      )}
      {status === "failed" && onRetry && (
        <button
          onClick={onRetry}
          disabled={retrying}
          className="rounded border border-red-700 px-3 py-1 text-xs text-red-200 hover:border-red-500 disabled:opacity-50"
        >
          {retrying ? "重试中…" : "从断点重试"}
        </button>
      )}
    </div>
  );
}