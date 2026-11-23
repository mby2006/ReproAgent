"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { apiFetch } from "@/lib/api";
import { supabase } from "@/lib/supabase";
import type { WorkflowDetail } from "@/lib/types";
import { TONE_DOT, WF_META } from "@/lib/ui";
import {
  IconGrid,
  IconFlask,
  IconLogout,
  IconMenu,
  IconPapers,
  IconPulse,
  IconX,
  LogoMark,
} from "./icons";

const NAV = [
  { href: "/", label: "首页", icon: IconGrid },
  { href: "/examples/lenet-mnist", label: "公开实验", icon: IconFlask },
  { href: "/papers", label: "论文库", icon: IconPapers },
  { href: "/health", label: "服务健康", icon: IconPulse },
];

function isActive(pathname: string, href: string) {
  if (href === "/") return pathname === "/";
  if (href === "/papers") {
    return ["/papers", "/experiments", "/comparison"].some((p) => pathname.startsWith(p));
  }
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
      <Link href="/" onClick={onNavigate} className="wordmark px-1 pt-1">
        <LogoMark />ReproAgent
      </Link>

      <nav className="space-y-1" aria-label="工作台导航">
        {NAV.map((item) => {
          const active = isActive(pathname, item.href);
          const Icon = item.icon;
          return (
            <Link
              aria-current={active ? "page" : undefined}
              key={item.href}
              href={item.href}
              onClick={onNavigate}
              className={`nav-item ${active ? "nav-item-active" : ""}`}
            >
              <Icon className="h-4 w-4 shrink-0" />
              <span className="flex-1">{item.label}</span>
            </Link>
          );
        })}
      </nav>

      <div className="min-h-0 flex-1">
        <p className="label mb-2 px-1">最近的实验</p>
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
                    href={`/papers?id=${wf.paper_id}&workflow=${wf.id}`}
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
              className="link-quiet grid size-11 shrink-0 place-items-center"
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
  const drawer = useRef<HTMLDialogElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 1024px)");
    const closeOnDesktop = () => { if (desktop.matches) drawer.current?.close(); };
    desktop.addEventListener("change", closeOnDesktop);
    return () => desktop.removeEventListener("change", closeOnDesktop);
  }, []);

  useEffect(() => {
    if (open) closeButton.current?.focus();
  }, [open]);

  return (
    <div className="relative min-h-dvh">
      <a className="skip-link" href="#workspace-content">跳到工作区</a>

      <aside className="fixed inset-y-0 left-0 z-30 hidden w-64 border-r border-line-soft bg-ink-900/85 backdrop-blur-md lg:block">
        <SidebarBody />
      </aside>

      <header className="sticky top-0 z-30 flex items-center gap-3 border-b border-line-soft bg-ink-950/85 px-4 py-2.5 backdrop-blur-md lg:hidden">
        <button
          onClick={() => { drawer.current?.showModal(); setOpen(true); }}
          aria-controls="workspace-navigation"
          aria-expanded={open}
          className="btn btn-ghost btn-sm px-2!"
          aria-label="打开导航"
        >
          <IconMenu className="h-4 w-4" />
        </button>
        <Link href="/" className="wordmark"><LogoMark />ReproAgent</Link>
      </header>

      <dialog ref={drawer} id="workspace-navigation" className="workspace-drawer" aria-label="工作台导航"
        onClose={() => setOpen(false)} onClick={event => { if (event.target === event.currentTarget) drawer.current?.close(); }}>
        {open && <div className="relative h-full">
          <button ref={closeButton} type="button" className="drawer-close" aria-label="关闭导航" onClick={() => drawer.current?.close()}>
            <IconX />
          </button>
          <SidebarBody onNavigate={() => drawer.current?.close()} />
        </div>}
      </dialog>

      <div id="workspace-content" tabIndex={-1} className="relative z-10 lg:pl-64">{children}</div>
    </div>
  );
}
