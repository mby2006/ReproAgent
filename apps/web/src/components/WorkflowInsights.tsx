"use client";

import WorkflowStepper from "@/components/WorkflowStepper";
import { useEffect, useState } from "react";
import { getWorkflowTraces } from "@/lib/api";
import type { NodeTrace, WorkflowDetail } from "@/lib/types";
import { IconChevronDown } from "./icons";

function num(budget: Record<string, unknown>, key: string): number {
  const v = budget[key];
  return typeof v === "number" ? v : 0;
}

function BudgetBar({ budget }: { budget: Record<string, unknown> }) {
  // Every number here answers a question a non-specialist actually asks:
  // how much AI was used, how long the training took, how much self-repair happened.
  const items = [
    {
      label: "AI 调用次数",
      value: String(num(budget, "llm_calls")),
      hint: "读论文、给建议时调用大模型的次数",
    },
    {
      label: "AI 用量",
      value: num(budget, "total_tokens").toLocaleString(),
      hint: "消耗的 token 数，越大花费越高",
    },
    {
      label: "训练耗时",
      value: `${num(budget, "experiment_seconds").toFixed(1)} 秒`,
      hint: "沙箱里真实跑训练花的时间",
    },
    {
      label: "自动修复次数",
      value: String(num(budget, "repair_count")),
      hint: "代码报错后自动重试的次数",
    },
    {
      label: "重新规划次数",
      value: String(num(budget, "replan_count")),
      hint: "计划被推翻重做的次数",
    },
  ];
  return (
    <div className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-line bg-line sm:grid-cols-5">
      {items.map((item) => (
        <div key={item.label} className="bg-ink-900/90 px-3.5 py-3" title={item.hint}>
          <p className="text-[12px] text-fg-3">{item.label}</p>
          <p className="display mt-1 truncate text-xl font-semibold text-fg" title={item.value}>
            {item.value}
          </p>
        </div>
      ))}
    </div>
  );
}

const REVIEW_SOURCES: Record<string, string> = {
  human: "人工确认", ai_assisted: "AI 辅助，人工采纳", auto: "系统自动接受（非人工确认）",
};
const REVIEW_MODES: Record<string, string> = {
  expert: "专家审核", assisted: "AI 辅助审核", delegated: "托管审核",
};

function traceValue(value: unknown) {
  return value == null ? "未知" : typeof value === "string" ? value : JSON.stringify(value);
}

export function TraceRow({ trace }: { trace: NodeTrace }) {
  const review = trace.node_name === "review_decision";
  const output = trace.output_json;
  const source = REVIEW_SOURCES[String(output.confirmed_by)] ?? "来源未知 / 未确认";
  const tokens =
    trace.token_usage && typeof trace.token_usage.total_tokens === "number"
      ? trace.token_usage.total_tokens.toLocaleString()
      : null;
  return (
    <details id={`workflow-trace-${trace.id}`} className="group card scroll-mt-4 overflow-hidden">
      <summary className="flex cursor-pointer list-none flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm [&::-webkit-details-marker]:hidden">
        <IconChevronDown className="h-3.5 w-3.5 text-fg-3 transition group-open:rotate-180" />
        <span className={`h-1.5 w-1.5 rounded-full ${trace.error ? "bg-bad" : "bg-ok"}`} />
        <span className="font-mono text-[12px] text-fg">{review ? "审核决策" : trace.node_name}</span>
        {review && <span className="break-all text-xs text-fg-2">{traceValue(trace.input_json.field_path)} · {source}</span>}
        {trace.duration_ms !== null && (
          <span className="font-mono text-[12px] text-fg-3">{trace.duration_ms}ms</span>
        )}
        {trace.model_name && (
          <span className="truncate font-mono text-[12px] text-fg-3">{trace.model_name}</span>
        )}
        {tokens && <span className="font-mono text-[12px] text-fg-3">{tokens} tok</span>}
        {trace.retry_count > 0 && (
          <span className="chip badge-warn py-0!">retry {trace.retry_count}</span>
        )}
        <span className={`chip ml-auto py-0! ${trace.error ? "badge-bad" : review && output.confirmed_by === "auto" ? "badge-warn" : "badge-ok"}`}>
          {trace.error ? "ERR" : review ? "已记录" : "OK"}
        </span>
      </summary>
      <div className="space-y-2 border-t border-line-soft px-3 py-2">
        {trace.error && <p className="text-xs text-bad">{trace.error}</p>}
        {review ? (
          <dl className="grid gap-2 break-all text-xs sm:grid-cols-2">
            {[
              ["审核事件 ID", trace.id], ["字段", traceValue(trace.input_json.field_path)],
              ["旧值（已脱敏）", traceValue(trace.input_json.before)],
              ["新值（已脱敏）", traceValue(output.after)], ["确认来源", source],
              ["审核模式", REVIEW_MODES[String(output.review_mode)] ?? "未知"],
              ["操作用户", traceValue(output.actor)],
              ["时间", traceValue(output.timestamp ?? trace.created_at)],
              ["推荐 ID", traceValue(output.recommendation_id)],
              ["推荐置信度", traceValue(output.recommendation_confidence)],
              ["推荐风险", traceValue(output.recommendation_risk)],
              ["预选阈值", traceValue(output.preselection_threshold)],
              ["推荐理由（已脱敏）", traceValue(output.recommendation_reasoning)],
            ].map(([label, value]) => (
              <div key={label}><dt className="text-fg-3">{label}</dt><dd className="mt-1 whitespace-pre-wrap text-fg-2">{value}</dd></div>
            ))}
          </dl>
        ) : <div className="grid gap-2 lg:grid-cols-2">
          <div>
            <p className="label mb-1 text-[11px]!">input（已脱敏）</p>
            <pre className="max-h-48 overflow-auto rounded-lg bg-ink-950 p-2 font-mono text-[12px] leading-relaxed text-fg-2">
              {JSON.stringify(trace.input_json, null, 2)}
            </pre>
          </div>
          <div>
            <p className="label mb-1 text-[11px]!">output（已脱敏）</p>
            <pre className="max-h-48 overflow-auto rounded-lg bg-ink-950 p-2 font-mono text-[12px] leading-relaxed text-fg-2">
              {JSON.stringify(trace.output_json, null, 2)}
            </pre>
          </div>
        </div>}
      </div>
    </details>
  );
}

export default function WorkflowInsights({ workflow, onRetry, retrying }: {
  workflow: WorkflowDetail; onRetry?: () => void; retrying?: boolean;
}) {
  const [traces, setTraces] = useState<NodeTrace[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getWorkflowTraces(workflow.id)
      .then((list) => {
        if (!cancelled) setTraces(list);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "加载 Trace 失败");
      });
    return () => {
      cancelled = true;
    };
  }, [workflow.id, workflow.updated_at]);

  return (
    <section className="rise space-y-2">
      <WorkflowStepper status={workflow.status} currentNode={workflow.current_node}
        error={workflow.error} onRetry={onRetry} retrying={retrying} traces={traces} />
      <BudgetBar budget={workflow.budget_json} />
      <details className="card group overflow-hidden">
        <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-2 text-sm text-fg-2 [&::-webkit-details-marker]:hidden">
          <IconChevronDown className="h-3.5 w-3.5 text-fg-3 transition group-open:rotate-180" />
          <span className="label text-fg-2!">Node Trace</span>
          <span className="chip py-0!">{traces.length}</span>
          <span className="ml-auto font-mono text-[10px] text-fg-3">逐节点输入 / 输出 / 用量</span>
        </summary>
        <div className="space-y-1.5 border-t border-line-soft p-3">
          {error && <p className="text-sm text-bad">{error}</p>}
          {!error && traces.length === 0 && (
            <p className="text-sm text-fg-3">暂无节点记录。</p>
          )}
          {traces.map((trace) => (
            <TraceRow key={trace.id} trace={trace} />
          ))}
        </div>
      </details>
    </section>
  );
}