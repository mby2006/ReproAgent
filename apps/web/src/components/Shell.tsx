"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import { supabase } from "@/lib/supabase";
import type { WorkflowDetail } from "@/lib/types";
import { TONE_DOT, WF_META } from "@/lib/ui";
import {
  IconGrid,
  IconLogout,
  IconMenu,
  IconPapers,
  IconPulse,
  IconX,
  LogoMark,
} from "./icons";

const NAV = [
  { href: "/", label: "总览", en: "OVERVIEW", icon: IconGrid },
  { href: "/papers", label: "论文库", en: "PAPERS", icon: IconPapers },
  { href: "/health", label: "服务健康", en: "HEALTH", icon: IconPulse },
];

function isActive(pathname: string, href: string) {
  if (href === "/") return pathname === "/";
  return pathname.startsWith(href);
}

function SidebarBody({ onNavigate }: { onNavigate?: () => void }) {
  const pathname = usePathname();
  const router = useRouter();
  const [email, setEmail] = useState<string | null>(null);
  const [workflows, setWorkflows] = useState<WorkflowDetail[] | null>(null);

  const loadWorkflows = useCallback(async () => {
    const { data } = await supabase.auth.getSession();
    if (!data.session) {
      setEmail(null);
      setWorkflows(null);
      return;
    }
    setEmail(data.session.user.email ?? null);
    try {
      const list = await apiFetch<WorkflowDetail[]>("/v1/workflows");
      setWorkflows(list.slice(0, 4));
    } catch {
      setWorkflows(null);
    }
  }, []);

  useEffect(() => {
    void loadWorkflows();
    const timer = setInterval(() => void loadWorkflows(), 8000);
    return () => clearInterval(timer);
  }, [loadWorkflows, pathname]);

  return (
    <div className="flex h-full flex-col gap-6 overflow-y-auto p-4">
      <Link href="/" onClick={onNavigate} className="flex items-center gap-3 px-1 pt-1">
        <LogoMark className="h-9 w-9 shrink-0" />
        <span className="min-w-0">
          <span className="display block text-[17px] font-bold leading-tight tracking-wide">
            REPRO<span className="text-brand">/</span>AGENT
          </span>
          <span className="label block text-[9.5px]!">Evidence Lab · Phase 1</span>
        </span>
      </Link>

      <nav className="space-y-1">
        {NAV.map((item) => {
          const active = isActive(pathname, item.href);
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              onClick={onNavigate}
              className={`nav-item ${active ? "nav-item-active" : ""}`}
            >
              <Icon className="h-4 w-4 shrink-0" />
              <span className="flex-1">{item.label}</span>
              <span className="label text-[9px]! opacity-70">{item.en}</span>
            </Link>
          );
        })}
      </nav>

      <div className="min-h-0 flex-1">
        <p className="label mb-2 px-1">Live Workflows</p>
        {workflows === null ? (
          <p className="px-1 text-xs text-fg-3">登录后可见实时工作流。</p>
        ) : workflows.length === 0 ? (
          <p className="px-1 text-xs text-fg-3">暂无工作流，上传一篇论文开始。</p>
        ) : (
          <ul className="space-y-1">
            {workflows.map((wf) => {
              const meta = WF_META[wf.status];
              return (
                <li key={wf.id}>
                  <Link
                    href={`/papers/${wf.paper_id}?workflow=${wf.id}`}
                    onClick={onNavigate}
                    className="group flex items-center gap-2.5 rounded-lg border border-transparent px-2 py-1.5 transition hover:border-line hover:bg-ink-800"
                    title={`${meta.label} · ${wf.current_node ?? wf.status}`}
                  >
                    <span
                      className={`pulse-dot h-1.5 w-1.5 shrink-0 rounded-full ${TONE_DOT[meta.tone]}`}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-mono text-[11px] text-fg-2 group-hover:text-fg">
                        {wf.paper_id.slice(0, 8)}
                      </span>
                      <span className="block truncate text-[10px] text-fg-3">
                        {meta.label}
                        {wf.current_node ? ` · ${wf.current_node}` : ""}
                      </span>
                    </span>
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      <div className="border-t border-line-soft pt-3">
        {email ? (
          <div className="flex items-center gap-2 px-1">
            <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-brand/40 bg-brand/10 font-mono text-[11px] font-semibold text-brand">
              {email.slice(0, 1).toUpperCase()}
            </span>
            <span className="min-w-0 flex-1 truncate text-xs text-fg-2" title={email}>
              {email}
            </span>
            <button
              onClick={() => supabase.auth.signOut().then(() => router.replace("/login"))}
              className="link-quiet shrink-0"
              title="退出登录"
            >
              <IconLogout className="h-4 w-4" />
            </button>
          </div>
        ) : (
          <Link href="/login" onClick={onNavigate} className="btn btn-ghost btn-sm w-full">
            登录 / 注册
          </Link>
        )}
      </div>
    </div>
  );
}

export default function Shell({ children }: { children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();

  useEffect(() => {
    setOpen(false);
  }, [pathname]);

  return (
    <div className="relative min-h-dvh">
      <div className="ambient" />

      <aside className="fixed inset-y-0 left-0 z-30 hidden w-64 border-r border-line-soft bg-ink-900/85 backdrop-blur-md lg:block">
        <SidebarBody />
      </aside>

      <header className="sticky top-0 z-30 flex items-center gap-3 border-b border-line-soft bg-ink-950/85 px-4 py-2.5 backdrop-blur-md lg:hidden">
        <button
          onClick={() => setOpen(true)}
          className="btn btn-ghost btn-sm px-2!"
          aria-label="打开导航"
        >
          <IconMenu className="h-4 w-4" />
        </button>
        <LogoMark className="h-6 w-6" />
        <span className="display text-sm font-bold tracking-wide">
          REPRO<span className="text-brand">/</span>AGENT
        </span>
      </header>

      {open && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div
            className="absolute inset-0 bg-ink-950/70 backdrop-blur-sm"
            onClick={() => setOpen(false)}
          />
          <aside className="rise absolute inset-y-0 left-0 w-72 border-r border-line bg-ink-900 shadow-2xl">
            <button
              onClick={() => setOpen(false)}
              className="absolute right-3 top-3 z-10 text-fg-3 hover:text-fg"
              aria-label="关闭导航"
            >
              <IconX className="h-4 w-4" />
            </button>
            <SidebarBody onNavigate={() => setOpen(false)} />
          </aside>
        </div>
      )}

      <div className="relative z-10 lg:pl-64">{children}</div>
    </div>
  );
}