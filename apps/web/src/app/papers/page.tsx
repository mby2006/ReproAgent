"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import AuthGate from "@/components/AuthGate";
import { apiFetch, uploadPaper } from "@/lib/api";
import { supabase } from "@/lib/supabase";
import type { PaperSummary } from "@/lib/types";

const STATUS_LABEL: Record<PaperSummary["status"], string> = {
  uploaded: "解析中",
  parsed: "已解析",
  failed: "解析失败",
};

function PapersInner() {
  const router = useRouter();
  const [papers, setPapers] = useState<PaperSummary[]>([]);
  const [title, setTitle] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    apiFetch<PaperSummary[]>("/v1/papers")
      .then(setPapers)
      .catch((err) => setError(err.message));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

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

  return (
    <main className="mx-auto w-full max-w-3xl flex-1 space-y-8 p-6">
      <header className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">我的论文</h1>
        <button
          onClick={() => supabase.auth.signOut().then(() => router.replace("/login"))}
          className="text-sm text-zinc-400 hover:text-zinc-200"
        >
          退出登录
        </button>
      </header>

      <form
        onSubmit={submit}
        className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-900 p-5"
      >
        <h2 className="font-medium">上传 PDF 或提供 URL 开始复现理解</h2>
        <input
          type="file"
          accept="application/pdf,.pdf"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          className="block w-full text-sm text-zinc-400 file:mr-3 file:rounded file:border-0 file:bg-zinc-700 file:px-3 file:py-1.5 file:text-sm file:text-zinc-100"
        />
        <input
          type="url"
          placeholder="或论文 PDF URL（如 https://arxiv.org/abs/…，自动转 PDF）"
          value={url}
          disabled={file != null}
          onChange={(e) => setUrl(e.target.value)}
          className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm disabled:opacity-50"
        />
        <input
          type="text"
          placeholder="论文标题（可选，默认用文件名或 URL）"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm"
        />
        {error && <p className="text-sm text-red-400">{error}</p>}
        <button
          type="submit"
          disabled={busy || (!file && !url.trim())}
          className="rounded bg-emerald-600 px-4 py-2 text-sm font-medium hover:bg-emerald-500 disabled:opacity-50"
        >
          {busy ? "提交中…" : "上传并解析"}
        </button>
      </form>

      <ul className="space-y-2">
        {papers.map((paper) => (
          <li key={paper.id}>
            <Link
              href={`/papers/${paper.id}`}
              className="flex items-center justify-between rounded-lg border border-zinc-800 bg-zinc-900 px-4 py-3 hover:border-zinc-600"
            >
              <span className="truncate">{paper.title}</span>
              <span
                className={`ml-3 shrink-0 text-xs ${
                  paper.status === "parsed"
                    ? "text-emerald-400"
                    : paper.status === "failed"
                      ? "text-red-400"
                      : "text-amber-400"
                }`}
              >
                {STATUS_LABEL[paper.status]}
              </span>
            </Link>
          </li>
        ))}
        {papers.length === 0 && (
          <li className="text-sm text-zinc-500">还没有论文，先上传一篇 PDF。</li>
        )}
      </ul>
    </main>
  );
}

export default function PapersPage() {
  return (
    <AuthGate>
      <PapersInner />
    </AuthGate>
  );
}