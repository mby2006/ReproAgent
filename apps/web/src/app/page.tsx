"use client";

import Link from "next/link";
import { useEffect } from "react";
import {
  IconArrowRight,
  IconChip,
  IconCode,
  IconFlask,
  IconLock,
  IconPapers,
  IconPulse,
  IconQuote,
  LogoMark,
} from "@/components/icons";

const PIPELINE = [
  { step: "01", title: "理解论文", desc: "解析 PDF / URL，理解方法与实验设置", icon: IconPapers },
  { step: "02", title: "提取规格", desc: "结构化 Reproduction Card，逐字段原文证据", icon: IconQuote },
  { step: "03", title: "实验计划", desc: "缩小规模的可执行计划，偏差与假设显式声明", icon: IconFlask },
  { step: "04", title: "生成代码", desc: "确定性模板生成 Python 训练代码 + 静态校验", icon: IconCode },
  { step: "05", title: "隔离执行", desc: "无密钥沙箱容器运行，Agent 不接触执行", icon: IconLock },
  { step: "06", title: "对比报告", desc: "与论文结果逐项比较，产出可追溯复现报告", icon: IconPulse },
];

const EVIDENCE = [
  {
    tone: "badge-ok",
    dot: "bg-ok",
    name: "paper_explicit",
    label: "原文",
    desc: "值直接来自论文，附原文引文、页码与章节，可点击跳转到 PDF 对应页核对。",
  },
  {
    tone: "badge-warn",
    dot: "bg-warn",
    name: "inferred",
    label: "推断",
    desc: "论文未明说、由 Agent 推断的值。必须人工确认后才进入计划，置信度全程可见。",
  },
  {
    tone: "badge-mute",
    dot: "bg-fg-3",
    name: "missing",
    label: "缺失",
    desc: "论文中找不到的信息绝不伪造：标记缺失，进入假设提案或已知局限清单。",
  },
];

const STATS = [
  { value: "15", label: "编排节点" },
  { value: "12", label: "工作流状态" },
  { value: "0", label: "沙箱持有密钥" },
  { value: "100%", label: "实验值来自真实运行" },
];

function useReveal() {
  useEffect(() => {
    const els = Array.from(document.querySelectorAll<HTMLElement>("[data-reveal]"));
    const io = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) {
            const el = entry.target as HTMLElement;
            el.classList.add("rise");
            el.style.opacity = "1";
            io.unobserve(el);
          }
        }
      },
      { threshold: 0.12 },
    );
    for (const el of els) {
      el.style.opacity = "0";
      io.observe(el);
    }
    return () => io.disconnect();
  }, []);
}

function PipelineInstrument() {
  const nodes = PIPELINE.map((p) => p.title);
  return (
    <div className="relative">
      <svg viewBox="0 0 300 430" className="w-full max-w-105" aria-hidden>
        <defs>
          <linearGradient id="pipe" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--color-info)" />
            <stop offset="55%" stopColor="var(--color-brand)" />
            <stop offset="100%" stopColor="var(--color-scale)" />
          </linearGradient>
        </defs>
        <path
          d="M60 40 V390"
          stroke="url(#pipe)"
          strokeWidth="1.6"
          strokeDasharray="5 9"
          className="animate-dash"
          opacity="0.8"
        />
        {nodes.map((label, i) => {
          const y = 40 + i * 70;
          const active = i === 4;
          return (
            <g key={label}>
              {active && (
                <circle cx="60" cy={y} r="13" fill="none" stroke="var(--color-brand)" strokeWidth="1" className="animate-ping-soft" style={{ transformOrigin: `60px ${y}px` }} />
              )}
              <circle
                cx="60"
                cy={y}
                r="7"
                fill={active ? "var(--color-brand)" : "var(--color-ink-800)"}
                stroke={active ? "var(--color-brand)" : i < 4 ? "var(--color-ok)" : "var(--color-line)"}
                strokeWidth="1.6"
              />
              {i < 4 && !active && <path d={`M56.5 ${y} l2.5 2.5 4.5 -5`} stroke="var(--color-ok)" strokeWidth="1.4" fill="none" />}
              <text x="86" y={y - 2} fill="var(--color-fg)" fontSize="13" fontWeight="600" fontFamily="var(--font-display)">
                {label}
              </text>
              <text x="86" y={y + 14} fill="var(--color-fg-3)" fontSize="9.5" fontFamily="var(--font-mono)" letterSpacing="1.5">
                {`NODE_${String(i + 1).padStart(2, "0")}`}
                {active ? " · RUNNING" : i < 4 ? " · DONE" : " · QUEUED"}
              </text>
            </g>
          );
        })}
      </svg>

      <div className="card animate-floaty absolute -right-2 top-6 w-52 p-3 shadow-xl sm:right-0">
        <p className="label mb-2 flex items-center justify-between">
          EvidenceField <span className="chip badge-mute py-0!">schema</span>
        </p>
        <div className="space-y-1.5 font-mono text-[10.5px] leading-relaxed text-fg-2">
          <p><span className="text-fg-3">value:</span> <span className="text-brand">string | num</span></p>
          <p><span className="text-fg-3">status:</span> <span className="text-ok">&quot;paper_explicit&quot;</span></p>
          <p><span className="text-fg-3">source_page:</span> <span className="text-info">int | null</span></p>
          <p><span className="text-fg-3">evidence_quote:</span> <span className="text-warn">str | null</span></p>
        </div>
        <div className="mt-2 flex gap-1.5">
          <span className="h-1.5 w-1.5 rounded-full bg-ok" />
          <span className="h-1.5 w-1.5 rounded-full bg-warn" />
          <span className="h-1.5 w-1.5 rounded-full bg-fg-3" />
        </div>
      </div>

      <div className="card animate-floaty absolute -left-3 bottom-10 w-44 p-3 shadow-xl [animation-delay:1.6s] sm:left-0">
        <p className="label mb-2">Sandbox</p>
        <p className="flex items-center gap-2 font-mono text-[10.5px] text-fg-2">
          <IconLock className="h-3.5 w-3.5 text-scale" /> keys: <span className="text-bad">none</span>
        </p>
        <p className="mt-1 flex items-center gap-2 font-mono text-[10.5px] text-fg-2">
          <IconChip className="h-3.5 w-3.5 text-info" /> net: <span className="text-warn">internal</span>
        </p>
      </div>
    </div>
  );
}

export default function Home() {
  useReveal();
  return (
    <div className="relative min-h-dvh overflow-x-clip">
      <div className="ambient" />

      <header className="sticky top-0 z-30 border-b border-line-soft bg-ink-950/80 backdrop-blur-md">
        <div className="mx-auto flex max-w-6xl items-center gap-3 px-5 py-3">
          <LogoMark className="h-7 w-7" />
          <span className="display text-[15px] font-bold tracking-wide">
            REPRO<span className="text-brand">/</span>AGENT
          </span>
          <span className="label hidden text-[9.5px]! sm:block">Evidence Lab</span>
          <nav className="ml-auto flex items-center gap-2">
            <Link href="/health" className="link-quiet px-2 py-1">服务健康</Link>
            <Link href="/papers" className="btn btn-primary btn-sm">
              进入工作台 <IconArrowRight className="h-3.5 w-3.5" />
            </Link>
          </nav>
        </div>
      </header>

      <main className="relative z-10 mx-auto max-w-6xl px-5">
        <section className="grid items-center gap-10 py-14 lg:grid-cols-[1.05fr_0.95fr] lg:py-20">
          <div>
            <p className="rise label mb-4 flex items-center gap-2 text-[10.5px]! text-brand!">
              <span className="pulse-dot inline-block h-1.5 w-1.5 rounded-full bg-brand" />
              PAPER → EVIDENCE → EXECUTION → REPORT
            </p>
            <h1 className="rise rise-1 display text-4xl font-bold leading-[1.12] tracking-tight sm:text-5xl lg:text-[3.4rem]">
              把论文变成
              <br />
              <span className="relative inline-block text-brand">
                可追溯
                <svg className="absolute -bottom-2 left-0 w-full" viewBox="0 0 200 9" preserveAspectRatio="none" aria-hidden>
                  <path d="M2 6.5C40 2 160 2 198 6" stroke="var(--color-brand)" strokeWidth="2.5" fill="none" strokeLinecap="round" opacity="0.65" />
                </svg>
              </span>
              的复现报告
            </h1>
            <p className="rise rise-2 mt-6 max-w-xl text-[15px] leading-relaxed text-fg-2">
              上传学术论文 PDF 或提供 URL。Agent 理解方法、提取带原文证据的复现规格、
              制定缩放计划、生成代码，并在<span className="text-fg">无密钥隔离沙箱</span>中执行，
              最终与论文结果逐项对比——每一个数字都能追到出处。
            </p>
            <div className="rise rise-3 mt-8 flex flex-wrap gap-3">
              <Link href="/papers" className="btn btn-primary">
                上传论文开始复现 <IconArrowRight className="h-4 w-4" />
              </Link>
              <Link href="/health" className="btn btn-ghost">
                <IconPulse className="h-4 w-4" /> 服务健康状态
              </Link>
            </div>
            <dl className="rise rise-4 mt-10 grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-line bg-line sm:grid-cols-4">
              {STATS.map((s) => (
                <div key={s.label} className="bg-ink-900/90 px-4 py-3">
                  <dt className="label order-2 mt-1 block text-[9.5px]!">{s.label}</dt>
                  <dd className="display text-2xl font-bold text-brand">{s.value}</dd>
                </div>
              ))}
            </dl>
          </div>
          <div className="rise rise-2 relative mx-auto w-full max-w-md lg:max-w-none">
            <PipelineInstrument />
          </div>
        </section>

        <section className="py-12" data-reveal>
          <div className="mb-8 flex items-end justify-between gap-4">
            <div>
              <p className="label mb-2">Pipeline · 六阶段</p>
              <h2 className="display text-2xl font-bold sm:text-3xl">一条证据链贯穿始终</h2>
            </div>
            <p className="hidden max-w-xs text-right text-xs leading-relaxed text-fg-3 sm:block">
              统一 LangGraph 工作流编排，Postgres checkpoint 断点续跑，逐节点 Trace 与预算消耗全程可见。
            </p>
          </div>
          <ol className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {PIPELINE.map((item, i) => {
              const Icon = item.icon;
              return (
                <li key={item.step} className="card card-hover group relative overflow-hidden p-5" data-reveal style={{ animationDelay: `${i * 0.07}s` }}>
                  <span className="display pointer-events-none absolute -right-2 -top-4 text-[64px] font-bold leading-none text-ink-700/60 transition group-hover:text-brand/15">
                    {item.step}
                  </span>
                  <span className="mb-4 flex h-9 w-9 items-center justify-center rounded-lg border border-brand/30 bg-brand/10 text-brand">
                    <Icon className="h-4.5 w-4.5" />
                  </span>
                  <h3 className="display text-base font-semibold">{item.title}</h3>
                  <p className="mt-1.5 text-[13px] leading-relaxed text-fg-2">{item.desc}</p>
                </li>
              );
            })}
          </ol>
        </section>

        <section className="py-12" data-reveal>
          <div className="mb-8">
            <p className="label mb-2">Evidence Contract · 证据契约</p>
            <h2 className="display text-2xl font-bold sm:text-3xl">不伪造任何一个数字</h2>
          </div>
          <div className="grid gap-4 lg:grid-cols-3">
            {EVIDENCE.map((e) => (
              <div key={e.name} className="card card-hover p-5" data-reveal>
                <div className="mb-3 flex items-center justify-between">
                  <span className={`chip ${e.tone}`}>
                    <span className={`h-1.5 w-1.5 rounded-full ${e.dot}`} />
                    {e.label}
                  </span>
                  <code className="font-mono text-[10.5px] text-fg-3">{e.name}</code>
                </div>
                <p className="text-[13px] leading-relaxed text-fg-2">{e.desc}</p>
              </div>
            ))}
          </div>
          <div className="card mt-4 flex flex-wrap items-center gap-x-8 gap-y-3 border-scale/25 bg-scale/5 px-5 py-4" data-reveal>
            <p className="flex items-center gap-2 text-[13px] font-semibold text-scale">
              <IconLock className="h-4 w-4" /> 信任域分离
            </p>
            <p className="font-mono text-[11px] text-fg-2">api / worker → Supabase keys</p>
            <p className="font-mono text-[11px] text-fg-2">agent → LLM key only</p>
            <p className="font-mono text-[11px] text-fg-2">runner → <span className="text-bad">no keys</span> · internal network</p>
            <p className="ml-auto text-[11px] text-fg-3">全表行级安全（RLS），用户数据彼此隔离</p>
          </div>
        </section>

        <footer className="flex flex-wrap items-center gap-3 border-t border-line-soft py-8 text-xs text-fg-3">
          <LogoMark className="h-5 w-5" />
          <span>ReproAgent · Phase 1 · LeNet-5 + MNIST</span>
          <span className="ml-auto font-mono">FastAPI · LangGraph · Next.js · Supabase · Docker sandbox</span>
        </footer>
      </main>
    </div>
  );
}