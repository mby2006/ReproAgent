"use client";

import { useEffect, useState } from "react";
import { getWorkflowTraces } from "@/lib/api";
import type { NodeTrace, WorkflowDetail } from "@/lib/types";

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
    <div className="flex flex-wrap gap-2">
      {items.map((item) => (
        <span
          key={item.label}
          className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-1 text-xs text-zinc-400"
        >
          {item.label} <span className="font-semibold text-zinc-200">{item.value}</span>
        </span>
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
    <details className="group rounded-lg border border-zinc-800 bg-zinc-900">
      <summary className="flex cursor-pointer flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2 text-sm">
        <span className="font-mono text-zinc-200">{trace.node_name}</span>
        {trace.duration_ms !== null && (
          <span className="text-xs text-zinc-500">{trace.duration_ms}ms</span>
        )}
        {trace.model_name && <span className="text-xs text-zinc-500">{trace.model_name}</span>}
        {tokens && <span className="text-xs text-zinc-500">{tokens} tokens</span>}
        {trace.retry_count > 0 && (
          <span className="rounded bg-amber-900 px-1.5 text-xs text-amber-200">
            retry {trace.retry_count}
          </span>
        )}
        {trace.error ? (
          <span className="rounded bg-red-900 px-1.5 text-xs text-red-200">错误</span>
        ) : (
          <span className="rounded bg-emerald-900 px-1.5 text-xs text-emerald-200">OK</span>
        )}
      </summary>
      <div className="space-y-2 border-t border-zinc-800 px-3 py-2">
        {trace.error && <p className="text-xs text-red-300">{trace.error}</p>}
        <div className="grid gap-2 lg:grid-cols-2">
          <div>
            <p className="mb-1 text-xs text-zinc-500">input（已脱敏）</p>
            <pre className="max-h-48 overflow-auto rounded bg-zinc-950 p-2 text-xs text-zinc-400">
              {JSON.stringify(trace.input_json, null, 2)}
            </pre>
          </div>
          <div>
            <p className="mb-1 text-xs text-zinc-500">output（已脱敏）</p>
            <pre className="max-h-48 overflow-auto rounded bg-zinc-950 p-2 text-xs text-zinc-400">
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
    <section className="space-y-2">
      <BudgetBar budget={workflow.budget_json} />
      <details className="rounded-xl border border-zinc-800 bg-zinc-900/60">
        <summary className="cursor-pointer px-4 py-2 text-sm text-zinc-300">
          节点 Trace（{traces.length}）
        </summary>
        <div className="space-y-1.5 border-t border-zinc-800 p-3">
          {error && <p className="text-sm text-red-300">{error}</p>}
          {!error && traces.length === 0 && (
            <p className="text-sm text-zinc-500">暂无节点记录。</p>
          )}
          {traces.map((trace) => (
            <TraceRow key={trace.id} trace={trace} />
          ))}
        </div>
      </details>
    </section>
  );
}