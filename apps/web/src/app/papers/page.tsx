"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import AuthGate from "@/components/AuthGate";
import Shell from "@/components/Shell";
import {
  IconArrowRight,
  IconLink,
  IconPdf,
  IconRefresh,
  IconSearch,
  IconUpload,
  IconX,
} from "@/components/icons";
import { apiFetch, uploadPaper } from "@/lib/api";
import type { PaperSummary } from "@/lib/types";
import { fmtDate, TONE_BADGE, type Tone } from "@/lib/ui";

const STATUS_META: Record<PaperSummary["status"], { label: string; tone: Tone }> = {
  uploaded: { label: "解析中", tone: "info" },
  parsed: { label: "已解析", tone: "ok" },
  failed: { label: "解析失败", tone: "bad" },
};

function PapersInner() {
  const router = useRouter();
  const [papers, setPapers] = useState<PaperSummary[]>([]);
  const [title, setTitle] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [drag, setDrag] = useState(false);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<"all" | PaperSummary["status"]>("all");
  const [sort, setSort] = useState<"newest" | "oldest" | "title">("newest");
  const fileInput = useRef<HTMLInputElement>(null);

  const load = useCallback(() => {
    apiFetch<PaperSummary[]>("/v1/papers")
      .then(setPapers)
      .catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  function pickFile(f: File | null) {
    if (!f) return;
    if (f.type !== "application/pdf" && !f.name.toLowerCase().endsWith(".pdf")) {
      setError("仅支持 PDF 文件");
      return;
    }
    setError(null);
    setFile(f);
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (!file && !url.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const result = await uploadPaper(
        file,
        title || (file ? file.name : ""),
        file ? undefined : url.trim()
      );
      router.push(`/papers/${result.paper_id}?workflow=${result.workflow_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "上传失败");
      setBusy(false);
    }
  }

  const visible = useMemo(() => {
    const q = query.trim().toLowerCase();
    const list = papers.filter(
      (p) =>
        (statusFilter === "all" || p.status === statusFilter) &&
        (q === "" || p.title.toLowerCase().includes(q) || p.id.includes(q)),
    );
    return list.sort((a, b) => {
      if (sort === "title") return a.title.localeCompare(b.title);
      const d = a.created_at.localeCompare(b.created_at);
      return sort === "newest" ? -d : d;
    });
  }, [papers, query, statusFilter, sort]);

  return (
    <main className="mx-auto w-full max-w-6xl space-y-6 px-5 py-7">
      <header className="rise flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="label mb-1.5">Library · 论文库</p>
          <h1 className="display flex items-center gap-3 text-3xl font-bold">
            我的论文
            <span className="chip badge-brand">{papers.length}</span>
          </h1>
        </div>
        <button onClick={load} className="btn btn-ghost btn-sm">
          <IconRefresh className="h-3.5 w-3.5" /> 刷新
        </button>
      </header>

      <form onSubmit={submit} className="card rise rise-1 grid gap-5 p-5 lg:grid-cols-[1.1fr_1fr]">
        <div
          className={`dropzone flex min-h-44 flex-col items-center justify-center gap-3 p-6 text-center ${drag ? "dropzone-active" : ""}`}
          onClick={() => fileInput.current?.click()}
          onDragOver={(e) => {
            e.preventDefault();
            setDrag(true);
          }}
          onDragLeave={() => setDrag(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDrag(false);
            pickFile(e.dataTransfer.files?.[0] ?? null);
          }}
          role="button"
          tabIndex={0}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") fileInput.current?.click();
          }}
        >
          <input
            ref={fileInput}
            type="file"
            accept="application/pdf,.pdf"
            className="hidden"
            onChange={(e) => pickFile(e.target.files?.[0] ?? null)}
          />
          {file ? (
            <>
              <span className="chip badge-brand rounded-full! px-4! py-2! text-xs!">
                <IconPdf className="h-4 w-4" />
                <span className="max-w-56 truncate">{file.name}</span>
                <span className="text-fg-3">{(file.size / 1024 / 1024).toFixed(2)} MB</span>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setFile(null);
                    if (fileInput.current) fileInput.current.value = "";
                  }}
                  className="ml-1 text-fg-3 hover:text-bad"
                  aria-label="移除文件"
                >
                  <IconX className="h-3.5 w-3.5" />
                </button>
              </span>
              <p className="text-xs text-fg-3">已选择文件，提交后开始解析</p>
            </>
          ) : (
            <>
              <span className="flex h-14 w-14 items-center justify-center rounded-full border border-brand/30 bg-brand/10 text-brand">
                <IconUpload className="h-6 w-6" />
              </span>
              <p className="text-sm font-medium text-fg-2">拖拽 PDF 到此处</p>
              <p className="text-xs text-brand">或点击选择文件</p>
            </>
          )}
        </div>

        <div className="flex flex-col gap-3">
          <label className="block space-y-1.5">
            <span className="label flex items-center gap-1.5">
              <IconLink className="h-3 w-3" /> 或论文 PDF URL
            </span>
            <input
              type="url"
              placeholder="https://arxiv.org/abs/…（自动转 PDF）"
              value={url}
              disabled={file != null}
              onChange={(e) => setUrl(e.target.value)}
              className="input"
            />
          </label>
          <label className="block space-y-1.5">
            <span className="label">标题（可选）</span>
            <input
              type="text"
              placeholder="默认用文件名或 URL"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              className="input"
            />
          </label>
          {error && (
            <p className="rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-xs text-bad">
              {error}
            </p>
          )}
          <button
            type="submit"
            disabled={busy || (!file && !url.trim())}
            className="btn btn-primary mt-auto"
          >
            <IconUpload className="h-4 w-4" />
            {busy ? "提交中…" : "上传并解析"}
          </button>
        </div>
      </form>

      <div className="rise rise-2 flex flex-wrap items-center gap-2">
        <div className="relative min-w-52 flex-1">
          <IconSearch className="pointer-events-none absolute left-3 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-fg-3" />
          <input
            type="search"
            placeholder="搜索标题或 ID…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="input pl-9!"
          />
        </div>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as typeof statusFilter)}
          className="select w-36!"
          aria-label="按状态筛选"
        >
          <option value="all">全部状态</option>
          <option value="parsed">已解析</option>
          <option value="uploaded">解析中</option>
          <option value="failed">解析失败</option>
        </select>
        <select
          value={sort}
          onChange={(e) => setSort(e.target.value as typeof sort)}
          className="select w-36!"
          aria-label="排序"
        >
          <option value="newest">最新优先</option>
          <option value="oldest">最早优先</option>
          <option value="title">按标题</option>
        </select>
      </div>

      {visible.length === 0 ? (
        <div className="rise rise-3 flex flex-col items-center gap-3 rounded-xl border border-dashed border-line py-16 text-center">
          <IconPdf className="h-10 w-10 text-fg-3" />
          <p className="text-sm text-fg-2">
            {papers.length === 0 ? "还没有论文，先上传一篇 PDF。" : "没有匹配的论文。"}
          </p>
          <p className="text-xs text-fg-3">支持本地 PDF 与 arXiv 等论文 URL</p>
        </div>
      ) : (
        <ul className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {visible.map((paper, i) => {
            const meta = STATUS_META[paper.status];
            return (
              <li key={paper.id} className="rise" style={{ animationDelay: `${Math.min(i, 8) * 0.05}s` }}>
                <Link href={`/papers/${paper.id}`} className="card card-hover group flex h-full flex-col p-4">
                  <div className="mb-3 flex items-center justify-between gap-2">
                    <span className="chip">
                      {paper.source === "upload" ? (
                        <IconPdf className="h-3 w-3 text-info" />
                      ) : (
                        <IconLink className="h-3 w-3 text-scale" />
                      )}
                      {paper.source === "upload" ? "PDF" : "URL"}
                    </span>
                    <span className={`chip ${TONE_BADGE[meta.tone]}`}>
                      <span className={`h-1.5 w-1.5 rounded-full ${meta.tone === "info" ? "pulse-dot bg-info" : meta.tone === "ok" ? "bg-ok" : "bg-bad"}`} />
                      {meta.label}
                    </span>
                  </div>
                  <h2 className="display line-clamp-2 min-h-10 text-[15px] font-semibold leading-snug group-hover:text-brand">
                    {paper.title}
                  </h2>
                  <div className="mt-auto flex items-center justify-between gap-2 pt-4">
                    <span className="font-mono text-[10.5px] text-fg-3">
                      {fmtDate(paper.created_at)} · {paper.id.slice(0, 8)}
                    </span>
                    <IconArrowRight className="h-4 w-4 text-fg-3 transition group-hover:translate-x-1 group-hover:text-brand" />
                  </div>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </main>
  );
}

export default function PapersPage() {
  return (
    <AuthGate>
      <Shell>
        <PapersInner />
      </Shell>
    </AuthGate>
  );
}