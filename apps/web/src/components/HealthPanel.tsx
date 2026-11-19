"use client";

import { useCallback, useEffect, useState } from "react";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

type Status = "ok" | "degraded" | "unavailable" | "unknown";

interface Dependency {
  name: string;
  status: Status;
  latency_ms: number | null;
  detail: string | null;
}

interface ServiceCard {
  name: string;
  status: Status;
  detail?: string | null;
}

const STATUS_STYLE: Record<Status, { dot: string; label: string }> = {
  ok: { dot: "bg-emerald-400", label: "正常" },
  degraded: { dot: "bg-amber-400", label: "降级" },
  unavailable: { dot: "bg-red-400", label: "不可用" },
  unknown: { dot: "bg-zinc-500", label: "未知" },
};

export default function HealthPanel() {
  const [cards, setCards] = useState<ServiceCard[]>([
    { name: "web", status: "ok", detail: "Next.js 前端" },
    { name: "api", status: "unknown" },
    { name: "agent", status: "unknown" },
    { name: "supabase", status: "unknown" },
    { name: "runner", status: "unknown" },
  ]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [healthRes, depsRes] = await Promise.all([
        fetch(`${API_URL}/health`, { cache: "no-store" }),
        fetch(`${API_URL}/health/dependencies`, { cache: "no-store" }),
      ]);
      if (!healthRes.ok || !depsRes.ok) {
        throw new Error(`API 返回 ${healthRes.status}/${depsRes.status}`);
      }
      const deps: { dependencies: Dependency[] } = await depsRes.json();
      setError(null);
      setCards((prev) =>
        prev.map((card) => {
          if (card.name === "web") return card;
          if (card.name === "api") return { ...card, status: "ok" };
          const dep = deps.dependencies.find((d) => d.name === card.name);
          if (!dep) return { ...card, status: "unknown" };
          return {
            ...card,
            status: dep.status,
            detail:
              dep.detail ??
              (dep.latency_ms != null ? `${dep.latency_ms.toFixed(0)} ms` : null),
          };
        }),
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setCards((prev) =>
        prev.map((card) =>
          card.name === "web" ? card : { ...card, status: "unavailable" },
        ),
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => void refresh(), 0);
    return () => clearTimeout(timer);
  }, [refresh]);

  return (
    <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-5">
      <div className="mb-4 flex items-center justify-between">
        <h2 className="text-lg font-semibold">服务健康状态</h2>
        <button
          onClick={() => {
            setLoading(true);
            void refresh();
          }}
          disabled={loading}
          className="rounded-lg border border-zinc-700 px-3 py-1.5 text-sm text-zinc-300 transition hover:border-emerald-500 hover:text-emerald-400 disabled:opacity-50"
        >
          {loading ? "检测中…" : "刷新"}
        </button>
      </div>
      {error && (
        <p className="mb-3 rounded-lg bg-red-500/10 px-3 py-2 text-sm text-red-400">
          无法连接 API（{API_URL}）：{error}
        </p>
      )}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        {cards.map((card) => {
          const style = STATUS_STYLE[card.status];
          return (
            <div
              key={card.name}
              className="rounded-lg border border-zinc-800 bg-zinc-950/60 p-3"
            >
              <div className="flex items-center gap-2">
                <span className={`h-2.5 w-2.5 rounded-full ${style.dot}`} />
                <span className="font-mono text-sm font-medium">{card.name}</span>
              </div>
              <p className="mt-1.5 text-xs text-zinc-400">{style.label}</p>
              {card.detail && (
                <p className="mt-0.5 truncate text-xs text-zinc-500" title={card.detail}>
                  {card.detail}
                </p>
              )}
            </div>
          );
        })}
      </div>
      <p className="mt-3 text-xs text-zinc-500">
        runner 位于隔离网络，仅 agent 可达；其状态由 api → agent → runner 链路探测。
      </p>
    </section>
  );
}