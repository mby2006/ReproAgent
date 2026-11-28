"use client";

import { useEffect, useMemo, useState } from "react";
import { apiDownload, apiFetch, apiHref } from "@/lib/api";
import { EVIDENCE_CATEGORIES, groupEvidenceItems } from "@/lib/evidence-index.mjs";
import type { EvidenceIndex as EvidenceIndexData, EvidenceIndexItem } from "@/lib/types";

const CATEGORY_META = {
  paper: { title: "论文与出处", hint: "只指向真实论文资源；页码链接打开原 PDF。" },
  plan: { title: "批准计划", hint: "绑定最终尝试实际执行的不可变计划版本。" },
  code: { title: "代码与配置", hint: "仅列出最终尝试 pin 的代码文件与 SHA-256。" },
  execution: { title: "执行与产物", hint: "运行、指标、产物及环境指纹均属于最终尝试。" },
  review_report: { title: "审核与报告", hint: "最新不可变报告；审核 trace 仅来自持久化事件。" },
} as const;

const TARGET_LABEL: Record<string, string> = {
  paper: "论文", plan: "计划版本", generated_file: "代码文件", run: "运行",
  artifact: "训练产物", metrics: "指标", image: "执行镜像", dataset: "数据集",
  report: "实验报告", review_trace: "审核记录",
};

export function evidenceLocatorText(item: EvidenceIndexItem) {
  if (item.target_type === "generated_file" || item.target_type === "artifact") {
    return String(item.locator.path ?? item.locator.name ?? item.target_id);
  }
  if (item.target_type === "plan" || item.target_type === "report") {
    const version = item.locator.version;
    return typeof version === "number" || (typeof version === "string" && version)
      ? `v${version}`
      : item.target_id;
  }
  if (item.target_type === "review_trace") return `${item.locator.source} · ${item.target_id}`;
  return item.target_id;
}

function Hash({ value }: { value: string | null }) {
  if (!value) return null;
  return <code className="block break-all font-mono text-[11px] text-fg-3" title={value}>{value.slice(0, 12)}…</code>;
}

export async function loadPrivateEvidence({
  runId,
  item,
  fetchEvidence = apiFetch,
  downloadArtifact = apiDownload,
}: {
  runId: string;
  item: EvidenceIndexItem;
  fetchEvidence?: typeof apiFetch;
  downloadArtifact?: typeof apiDownload;
}): Promise<{ title: string; content: string } | null> {
  if (item.target_type === "generated_file") {
    const attempt = item.locator.attempt;
    if (typeof attempt !== "number") throw new Error("最终尝试的代码定位不可用");
    const files = await fetchEvidence<Array<{ path: string; content: string | null }>>(
      `/v1/runs/${runId}/files?attempt=${attempt}`,
    );
    const file = files.find((candidate) => candidate.path === item.locator.path);
    if (!file) throw new Error("最终尝试的代码文件不可用");
    return { title: file.path, content: file.content ?? "文件内容为空" };
  }
  if (item.target_type === "report") {
    const version = item.locator.version;
    if (typeof version !== "number") throw new Error("报告版本定位不可用");
    const report = await fetchEvidence<{ markdown: string }>(
      `/v1/runs/${runId}/report?version=${encodeURIComponent(String(version))}`,
    );
    return { title: `报告 v${version}`, content: report.markdown };
  }
  if (item.target_type === "artifact") {
    await downloadArtifact(
      `/v1/runs/${runId}/artifacts/${encodeURIComponent(item.target_id)}`,
      String(item.locator.name ?? "artifact"),
    );
    return null;
  }
  return null;
}

export default function EvidenceIndex({
  index,
  pdfUrl = null,
  fileBaseUrl,
}: {
  index: EvidenceIndexData | null;
  pdfUrl?: string | null;
  fileBaseUrl?: string;
}) {
  const [pdfs, setPdfs] = useState<Record<string, string>>({});
  const [panel, setPanel] = useState<{ title: string; content: string } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const grouped = useMemo(() => groupEvidenceItems(index?.items ?? []), [index]);
  const paperIds = useMemo(() => [...new Set((index?.items ?? [])
    .filter((item) => item.category === "paper")
    .map((item) => String(item.locator.paper_id ?? ""))
    .filter(Boolean))], [index]);

  useEffect(() => {
    if (fileBaseUrl || paperIds.length === 0) return;
    const controller = new AbortController();
    for (const paperId of paperIds) {
      void apiFetch<{ url: string }>(`/v1/papers/${paperId}/pdf`, { signal: controller.signal })
        .then((result) => {
          if (!controller.signal.aborted) setPdfs((previous) => ({ ...previous, [paperId]: apiHref(result.url) }));
        })
        .catch(() => {});
    }
    return () => controller.abort();
  }, [fileBaseUrl, paperIds]);

  async function showPrivate(item: EvidenceIndexItem) {
    if (!index || item.unavailable_reason) return;
    setBusy(item.id);
    setError(null);
    setPanel(null);
    try {
      const result = await loadPrivateEvidence({ runId: index.run_id, item });
      if (result) setPanel(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "证据加载失败");
    } finally {
      setBusy(null);
    }
  }

  function action(item: EvidenceIndexItem) {
    if (item.category === "paper") {
      const page = typeof item.locator.page === "number" ? item.locator.page : null;
      const url = pdfUrl ?? pdfs[String(item.locator.paper_id)] ?? (typeof item.locator.source_url === "string" ? item.locator.source_url : null);
      if (url) {
        return <a className="btn btn-ghost btn-sm" href={`${url}${page ? `#page=${page}` : ""}`} target="_blank" rel="noreferrer">打开原文{page ? ` · 第 ${page} 页` : ""}</a>;
      }
      return <span className="text-[12px] text-warn">{item.unavailable_reason ?? "PDF 地址不可用"}</span>;
    }
    if (item.unavailable_reason) return <span className="text-[12px] text-warn">不可用</span>;
    if (fileBaseUrl && ["plan", "generated_file", "artifact", "report"].includes(item.target_type)) {
      const file = String(item.locator.file ?? item.locator.path ?? item.locator.name ?? "");
      if (!file) return null;
      const href = `${fileBaseUrl.replace(/\/$/, "")}/${file.split("/").map(encodeURIComponent).join("/")}`;
      return <a className="btn btn-ghost btn-sm" href={href} download={item.target_type === "artifact" || item.target_type === "report"}>打开归档文件</a>;
    }
    if (["generated_file", "artifact", "report"].includes(item.target_type)) {
      return <button className="btn btn-ghost btn-sm" disabled={busy === item.id} onClick={() => void showPrivate(item)}>
        {busy === item.id ? "加载中…" : item.target_type === "artifact" ? "下载产物" : item.target_type === "report" ? "读取报告" : "读取代码"}
      </button>;
    }
    return null;
  }

  return <section id="run-evidence" className="card space-y-4 p-4" aria-labelledby="evidence-index-title">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div>
        <h2 id="evidence-index-title" className="display text-xl font-semibold">单次运行证据索引</h2>
        <p className="mt-1 text-[13px] text-fg-3">五类证据统一绑定最终尝试；缺失资源明确标记，不使用早期尝试补位。</p>
      </div>
      {index && <span className="chip font-mono">Run {index.run_id.slice(0, 12)}</span>}
    </div>
    {!index && <p role="status" className="text-sm text-fg-3">正在加载证据索引…</p>}
    {error && <p role="alert" className="rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-bad">{error}</p>}
    <div className="grid gap-3 xl:grid-cols-2">
      {EVIDENCE_CATEGORIES.map((category) => <section key={category} className="min-w-0 space-y-2 rounded-xl border border-line-soft bg-ink-950/40 p-3" aria-labelledby={`evidence-${category}`}>
        <div className="flex flex-wrap items-center gap-2">
          <h3 id={`evidence-${category}`} className="font-semibold text-fg">{CATEGORY_META[category].title}</h3>
          <span className="chip py-0! font-mono">{grouped[category].length}</span>
        </div>
        <p className="text-[12px] leading-relaxed text-fg-3">{CATEGORY_META[category].hint}</p>
        {grouped[category].length === 0 && <p className="text-[13px] text-fg-3">暂无此类证据。</p>}
        <ul className="space-y-2">
          {grouped[category].map((item) => <li key={item.id} className="min-w-0 rounded-lg border border-line-soft bg-ink-900/60 px-3 py-2">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="min-w-0">
                <span className="text-[11px] text-fg-3">{TARGET_LABEL[item.target_type] ?? item.target_type}</span>
                <p className="break-all font-mono text-[12.5px] text-fg-2" title={item.target_id}>{evidenceLocatorText(item)}</p>
              </div>
              {action(item)}
            </div>
            <Hash value={item.hash} />
            {item.unavailable_reason && <p className="mt-1 break-words text-[12px] text-warn">{item.unavailable_reason}</p>}
          </li>)}
        </ul>
      </section>)}
    </div>
    {panel && <div className="min-w-0 rounded-lg border border-line bg-ink-950/70 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <h3 className="break-all font-mono text-sm text-fg">{panel.title}</h3>
        <button className="btn btn-ghost btn-sm" onClick={() => setPanel(null)}>关闭</button>
      </div>
      <pre className="max-h-96 overflow-auto whitespace-pre-wrap break-words font-mono text-[12px] text-fg-2">{panel.content}</pre>
    </div>}
  </section>;
}
