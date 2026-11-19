"use client";

import { useEffect, useState } from "react";
import { getWorkflowTraces } from "@/lib/api";
import type { NodeTrace, WorkflowDetail } from "@/lib/types";
import { IconChevronDown } from "./icons";

function num(budget: Record<string, unknown>, key: string): number {
  const v = budget[key];
  return typeof v === "number" ? v : 0;
}

function BudgetBar({ budget }: { budget: Record<string, unknown> }) {
  const items = [
    { label: "LLM 调用", value: String(num(budget, "llm_calls")) },
    { label: "Tokens", value: num(budget, "total_tokens").toLocaleString() },
    { label: "实验时长", value: `${num(budget, "experiment_seconds").toFixed(1)}s` },
    { label: "自动修复", value: String(num(budget, "repair_count")) },
    { label: "重新规划", value: String(num(budget, "replan_count")) },
  ];
  return (
    <div className="grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-line bg-line sm:grid-cols-5">
      {items.map((item) => (
        <div key={item.label} className="bg-ink-900/90 px-3 py-2">
          <p className="label text-[9px]!">{item.label}</p>
          <p className="display mt-0.5 truncate text-lg font-semibold text-fg" title={item.value}>
            {item.value}
          </p>
        </div>
      ))}
    </div>
  );
}

function TraceRow({ trace }: { trace: NodeTrace }) {
  const tokens =
    trace.token_usage && typeof trace.token_usage.total_tokens === "number"
      ? trace.token_usage.total_tokens.toLocaleString()
      : null;
  return (
    <details className="group card overflow-hidden">
      <summary className="flex cursor-pointer list-none flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm [&::-webkit-details-marker]:hidden">
        <IconChevronDown className="h-3.5 w-3.5 text-fg-3 transition group-open:rotate-180" />
        <span className={`h-1.5 w-1.5 rounded-full ${trace.error ? "bg-bad" : "bg-ok"}`} />
        <span className="font-mono text-[12px] text-fg">{trace.node_name}</span>
        {trace.duration_ms !== null && (
          <span className="font-mono text-[10.5px] text-fg-3">{trace.duration_ms}ms</span>
        )}
        {trace.model_name && (
          <span className="truncate font-mono text-[10.5px] text-fg-3">{trace.model_name}</span>
        )}
        {tokens && <span className="font-mono text-[10.5px] text-fg-3">{tokens} tok</span>}
        {trace.retry_count > 0 && (
          <span className="chip badge-warn py-0!">retry {trace.retry_count}</span>
        )}
        <span className={`chip ml-auto py-0! ${trace.error ? "badge-bad" : "badge-ok"}`}>
          {trace.error ? "ERR" : "OK"}
        </span>
      </summary>
      <div className="space-y-2 border-t border-line-soft px-3 py-2">
        {trace.error && <p className="text-xs text-bad">{trace.error}</p>}
        <div className="grid gap-2 lg:grid-cols-2">
          <div>
            <p className="label mb-1 text-[9px]!">input（已脱敏）</p>
            <pre className="max-h-48 overflow-auto rounded-lg bg-ink-950 p-2 font-mono text-[10.5px] leading-relaxed text-fg-2">
              {JSON.stringify(trace.input_json, null, 2)}
            </pre>
          </div>
          <div>
            <p className="label mb-1 text-[9px]!">output（已脱敏）</p>
            <pre className="max-h-48 overflow-auto rounded-lg bg-ink-950 p-2 font-mono text-[10.5px] leading-relaxed text-fg-2">
              {JSON.stringify(trace.output_json, null, 2)}
            </pre>
          </div>
        </div>
      </div>
    </details>
  );
}

export default function WorkflowInsights({ workflow }: { workflow: WorkflowDetail }) {
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