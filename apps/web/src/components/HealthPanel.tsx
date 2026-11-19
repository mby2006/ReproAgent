"use client";

import { useCallback, useEffect, useState } from "react";
import { IconRefresh, IconWarn } from "@/components/icons";
import { TONE_BADGE, TONE_DOT, type Tone } from "@/lib/ui";

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

const STATUS_STYLE: Record<Status, { tone: Tone; label: string }> = {
  ok: { tone: "ok", label: "正常" },
  degraded: { tone: "warn", label: "降级" },
  unavailable: { tone: "bad", label: "不可用" },
  unknown: { tone: "mute", label: "未知" },
};

const SERVICE_NOTE: Record<string, string> = {
  web: "Next.js 前端",
  api: "FastAPI · 编排入口",
  agent: "LLM 解析 / 规划 / 评估",
  supabase: "Postgres + Storage + Auth",
  runner: "隔离沙箱执行器",
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

  const okCount = cards.filter((c) => c.status === "ok").length;

  return (
    <section className="card space-y-4 p-5">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="label">Service Health</h2>
        <span className={`chip ${okCount === cards.length ? "badge-ok" : okCount > 1 ? "badge-warn" : "badge-bad"}`}>
          {okCount}/{cards.length} 正常
        </span>
        <button
          onClick={() => {
            setLoading(true);
            void refresh();
          }}
          disabled={loading}
          className="btn btn-ghost btn-sm ml-auto"
        >
          <IconRefresh className={`h-3.5 w-3.5 ${loading ? "animate-spin" : ""}`} />
          {loading ? "检测中…" : "刷新"}
        </button>
      </div>

      {error && (
        <p className="flex items-start gap-2 rounded-lg border border-bad/30 bg-bad/8 px-4 py-2 text-sm text-bad">
          <IconWarn className="mt-0.5 h-4 w-4 shrink-0" />
          无法连接 API（{API_URL}）：{error}
        </p>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
        {cards.map((card, i) => {
          const style = STATUS_STYLE[card.status];
          return (
            <div
              key={card.name}
              className={`rise rise-${i + 1} rounded-lg border border-line bg-ink-900 p-3.5 transition-colors hover:border-line`}
            >
              <div className="flex items-center gap-2">
                <span className={`relative h-2.5 w-2.5 rounded-full ${TONE_DOT[style.tone]} ${card.status === "ok" ? "pulse-dot" : ""}`} />
                <span className="display text-sm font-semibold text-fg">{card.name}</span>
              </div>
              <span className={`chip mt-2 ${TONE_BADGE[style.tone]}`}>{style.label}</span>
              <p className="mt-2 truncate font-mono text-[11px] text-fg-3" title={card.detail ?? SERVICE_NOTE[card.name]}>
                {card.detail ?? SERVICE_NOTE[card.name]}
              </p>
            </div>
          );
        })}
      </div>

      <div className="rounded-lg border border-line-soft bg-ink-900/60 px-4 py-3">
        <p className="label mb-2">Trust Domains</p>
        <div className="grid gap-2 text-xs text-fg-2 sm:grid-cols-3">
          <p><span className="font-mono text-info">api / worker</span> — 持有 Supabase 密钥，负责全部 DB 写入与编排</p>
          <p><span className="font-mono text-scale">agent</span> — 仅持 LLM Key，不执行生成代码</p>
          <p><span className="font-mono text-brand">runner</span> — 无任何密钥，隔离网络，仅 agent 可达</p>
        </div>
        <p className="mt-2 text-[11px] text-fg-3">
          runner 状态由 api → agent → runner 链路探测；沙箱容器按运行创建，依赖为固定白名单。
        </p>
      </div>
    </section>
  );
}