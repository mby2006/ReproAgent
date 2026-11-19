"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import AuthGate from "@/components/AuthGate";
import WorkflowInsights from "@/components/WorkflowInsights";
import WorkflowStepper from "@/components/WorkflowStepper";
import { apiFetch, confirmCard, getPaperWorkflow, resumeWorkflow, retryWorkflow } from "@/lib/api";
import { useWorkflow } from "@/lib/useWorkflow";
import type { CardVersion, CardVersionMeta, EvidenceField, PaperSummary } from "@/lib/types";

interface Selection {
  label: string;
  path: string | null;
  field: EvidenceField;
}

function StatusBadge({ field }: { field: EvidenceField }) {
  if (field.status === "missing") {
    return <span className="rounded bg-zinc-700 px-1.5 py-0.5 text-xs text-zinc-300">缺失</span>;
  }
  if (field.status === "inferred") {
    return field.human_confirmed ? (
      <span className="rounded bg-sky-700 px-1.5 py-0.5 text-xs text-sky-100">推断·已确认</span>
    ) : (
      <span className="rounded bg-amber-600 px-1.5 py-0.5 text-xs text-amber-50">⚠ 推断</span>
    );
  }
  return field.human_confirmed ? (
    <span className="rounded bg-emerald-700 px-1.5 py-0.5 text-xs text-emerald-100">原文·已确认</span>
  ) : (
    <span className="rounded bg-emerald-800 px-1.5 py-0.5 text-xs text-emerald-200">原文</span>
  );
}

function FieldRow({
  label,
  field,
  active,
  onClick,
}: {
  label: string;
  field: EvidenceField;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`flex w-full items-center justify-between gap-2 rounded-lg border px-3 py-2 text-left text-sm ${
        active ? "border-emerald-500 bg-zinc-800" : "border-zinc-800 bg-zinc-900 hover:border-zinc-600"
      }`}
    >
      <span className="min-w-0">
        <span className="block text-xs text-zinc-400">{label}</span>
        <span className="block truncate">
          {field.status === "missing" ? "—" : String(field.value)}
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-1.5">
        {field.source_page !== null && (
          <span className="text-xs text-zinc-500">p.{field.source_page}</span>
        )}
        <StatusBadge field={field} />
      </span>
    </button>
  );
}

function PaperInner({ paperId }: { paperId: string }) {
  const search = useSearchParams();
  const [paper, setPaper] = useState<PaperSummary | null>(null);
  const [versions, setVersions] = useState<CardVersionMeta[]>([]);
  const [version, setVersion] = useState<number | null>(null);
  const [cardVersion, setCardVersion] = useState<CardVersion | null>(null);
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [selected, setSelected] = useState<Selection | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [continuing, setContinuing] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const editInput = useRef<HTMLInputElement>(null);
  const { workflowId, setWorkflowId, workflow, error: wfError, refresh } = useWorkflow(
    search.get("workflow")
  );
  const wfStatus = workflow?.status;

  const loadVersions = useCallback(async () => {
    const list = await apiFetch<CardVersionMeta[]>(`/v1/papers/${paperId}/versions`);
    setVersions(list);
    return list;
  }, [paperId]);

  const loadCard = useCallback(
    async (v: number | null) => {
      const query = v === null ? "" : `?version=${v}`;
      const card = await apiFetch<CardVersion>(`/v1/papers/${paperId}/card${query}`);
      setCardVersion(card);
      setSelected(null);
    },
    [paperId]
  );

  useEffect(() => {
    apiFetch<PaperSummary>(`/v1/papers/${paperId}`)
      .then(setPaper)
      .catch((err) => setError(err.message));
    apiFetch<{ url: string }>(`/v1/papers/${paperId}/pdf`)
      .then((r) => setPdfUrl(r.url))
      .catch(() => setPdfUrl(null));
    apiFetch<CardVersionMeta[]>(`/v1/papers/${paperId}/versions`)
      .then((list) => {
        setVersions(list);
        if (list.length > 0) return loadCard(null);
      })
      .catch(() => {});
  }, [paperId, loadCard]);

  useEffect(() => {
    if (search.get("workflow")) return;
    getPaperWorkflow(paperId)
      .then((wf) => setWorkflowId(wf.id))
      .catch(() => {});
  }, [paperId, search, setWorkflowId]);

  useEffect(() => {
    if (!wfStatus || wfStatus === "uploaded" || wfStatus === "parsing") return;
    loadVersions()
      .then((list) => {
        if (list.length > 0) return loadCard(null);
      })
      .catch(() => {});
    apiFetch<PaperSummary>(`/v1/papers/${paperId}`)
      .then(setPaper)
      .catch(() => {});
  }, [wfStatus, paperId, loadVersions, loadCard]);

  async function switchVersion(v: number) {
    setVersion(v);
    try {
      await loadCard(v);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载版本失败");
    }
  }

  async function reparse() {
    setError(null);
    try {
      const result = await apiFetch<{ workflow_id: string }>(`/v1/papers/${paperId}/reparse`, {
        method: "POST",
      });
      setWorkflowId(result.workflow_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "重新解析失败");
    }
  }

  async function confirmAndContinue() {
    if (!workflowId) return;
    setConfirming(true);
    setError(null);
    try {
      await confirmCard(paperId);
      await loadVersions();
      await loadCard(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "确认 Card 失败");
      setConfirming(false);
      return;
    }
    setConfirming(false);
    await continueWorkflow();
  }

  async function continueWorkflow() {
    if (!workflowId) return;
    setContinuing(true);
    setError(null);
    try {
      await resumeWorkflow(workflowId, "spec");
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "继续执行失败");
    } finally {
      setContinuing(false);
    }
  }

  async function retry() {
    if (!workflowId) return;
    setRetrying(true);
    setError(null);
    try {
      await retryWorkflow(workflowId);
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "重试失败");
    } finally {
      setRetrying(false);
    }
  }

  async function saveEdit() {
    if (!selected?.path || !cardVersion) return;
    const raw = editInput.current?.value ?? "";
    let value: string | number | boolean | null;
    if (raw.trim() === "") value = null;
    else if (typeof selected.field.value === "number") {
      const n = Number(raw);
      if (!Number.isFinite(n)) {
        setError("请输入有效数字");
        return;
      }
      value = n;
    } else if (typeof selected.field.value === "boolean") value = raw === "true";
    else value = raw;
    setSaving(true);
    setError(null);
    try {
      await apiFetch(`/v1/papers/${paperId}/card/edits`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_version: cardVersion.version,
          edits: [{ field_path: selected.path, value }],
        }),
      });
      await loadVersions();
      setVersion(null);
      await loadCard(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  const card = cardVersion?.card;
  const rows: { title: string; items: Selection[] }[] = [];
  if (card) {
    rows.push({
      title: "核心信息",
      items: [
        { label: "方法 Method", path: "method", field: card.method },
        { label: "模型架构 Architecture", path: "model_architecture", field: card.model_architecture },
        { label: "数据集 Dataset", path: "dataset", field: card.dataset },
        { label: "优化器 Optimizer", path: "optimizer", field: card.optimizer },
        { label: "损失函数 Loss", path: "loss_function", field: card.loss_function },
      ],
    });
    rows.push({
      title: "超参数 Hyperparameters",
      items: Object.entries(card.hyperparameters).map(([key, field]) => ({
        label: key,
        path: `hyperparameters.${key}`,
        field,
      })),
    });
    rows.push({
      title: "数据预处理 Preprocessing",
      items: Object.entries(card.data_preprocessing).map(([key, field]) => ({
        label: key,
        path: `data_preprocessing.${key}`,
        field,
      })),
    });
    rows.push({
      title: "评估指标 Metrics",
      items: card.evaluation_metrics.map((field, i) => ({
        label: `指标 ${i + 1}`,
        path: null,
        field,
      })),
    });
    rows.push({
      title: "论文报告结果 Reported Results",
      items: card.reported_results.map((r) => ({
        label: `${r.metric}${r.condition ? `（${r.condition}）` : ""}`,
        path: null,
        field: r,
      })),
    });
    rows.push({
      title: "基线 Baselines",
      items: card.baselines.map((b) => ({ label: b.name, path: null, field: b })),
    });
  }

  const busy =
    wfStatus != null &&
    !["awaiting_spec_review", "awaiting_plan_approval", "completed", "failed"].includes(wfStatus);
  const shownError = error ?? wfError;

  return (
    <main className="mx-auto w-full max-w-7xl flex-1 space-y-4 p-6">
      <header className="flex flex-wrap items-center gap-3">
        <Link href="/papers" className="text-sm text-zinc-400 hover:text-zinc-200">
          ← 返回
        </Link>
        <h1 className="min-w-0 flex-1 truncate text-xl font-semibold">
          {paper?.title ?? "论文"}
        </h1>
        {versions.length > 0 && (
          <select
            value={version ?? cardVersion?.version ?? ""}
            onChange={(e) => switchVersion(Number(e.target.value))}
            className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-sm"
          >
            {versions.map((v) => (
              <option key={v.id} value={v.version}>
                v{v.version} · {v.origin === "extraction" ? "解析" : "用户修改"}
              </option>
            ))}
          </select>
        )}
        <button
          onClick={reparse}
          disabled={busy}
          className="rounded border border-zinc-700 px-3 py-1 text-sm hover:border-zinc-500 disabled:opacity-50"
        >
          重新解析
        </button>
        {wfStatus === "awaiting_spec_review" &&
          (cardVersion?.origin === "user_edit" ? (
            <button
              onClick={continueWorkflow}
              disabled={continuing}
              className="rounded bg-sky-600 px-3 py-1 text-sm hover:bg-sky-500 disabled:opacity-50"
            >
              {continuing ? "提交中…" : "继续执行"}
            </button>
          ) : (
            <button
              onClick={confirmAndContinue}
              disabled={confirming || !card}
              className="rounded bg-emerald-600 px-3 py-1 text-sm hover:bg-emerald-500 disabled:opacity-50"
            >
              {confirming ? "提交中…" : "确认 Card 并继续"}
            </button>
          ))}
        {workflow?.experiment_id && (
          <Link
            href={`/experiments/${workflow.experiment_id}?workflow=${workflow.id}`}
            className="rounded bg-emerald-600 px-3 py-1 text-sm hover:bg-emerald-500"
          >
            查看实验计划 →
          </Link>
        )}
      </header>

      {workflow && (
        <WorkflowStepper
          status={workflow.status}
          currentNode={workflow.current_node}
          error={workflow.error}
          onRetry={retry}
          retrying={retrying}
        />
      )}
      {workflow && <WorkflowInsights workflow={workflow} />}
      {busy && (
        <p className="rounded-lg border border-amber-700 bg-amber-950 px-4 py-2 text-sm text-amber-300">
          工作流进行中…（{wfStatus}）
        </p>
      )}
      {wfStatus === "awaiting_spec_review" && (
        <p className="rounded-lg border border-sky-700 bg-sky-950 px-4 py-2 text-sm text-sky-300">
          请核对 Reproduction Card：推断值与缺失信息可在右侧确认或修改，然后点击「确认 Card 并继续」生成实验计划。
        </p>
      )}
      {wfStatus === "awaiting_plan_approval" && (
        <p className="rounded-lg border border-sky-700 bg-sky-950 px-4 py-2 text-sm text-sky-300">
          实验计划已生成，请前往实验页批准并执行。
        </p>
      )}
      {wfStatus === "completed" && (
        <p className="rounded-lg border border-emerald-700 bg-emerald-950 px-4 py-2 text-sm text-emerald-300">
          复现工作流已完成，可在实验页查看训练指标与 Reproduction Report。
        </p>
      )}
      {shownError && (
        <p className="rounded-lg border border-red-800 bg-red-950 px-4 py-2 text-sm text-red-300">
          {shownError}
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <section className="space-y-4">
          {rows.map((group) => (
            <div key={group.title} className="space-y-1.5">
              <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
                {group.title}
              </h2>
              {group.items.length === 0 && <p className="text-sm text-zinc-600">（无）</p>}
              {group.items.map((item) => (
                <FieldRow
                  key={item.label}
                  label={item.label}
                  field={item.field}
                  active={selected?.label === item.label}
                  onClick={() => setSelected(item)}
                />
              ))}
            </div>
          ))}
          {card && card.missing_information.length > 0 && (
            <div className="space-y-1.5">
              <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
                缺失信息 Missing Information
              </h2>
              <ul className="list-inside list-disc text-sm text-zinc-400">
                {card.missing_information.map((m) => (
                  <li key={m}>{m}</li>
                ))}
              </ul>
            </div>
          )}
          {!card && !busy && (
            <p className="text-sm text-zinc-500">暂无 Reproduction Card，点击“重新解析”开始。</p>
          )}
        </section>

        <section className="space-y-3">
          {selected ? (
            <div className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-900 p-4">
              <div className="flex items-center justify-between">
                <h3 className="font-medium">{selected.label}</h3>
                <StatusBadge field={selected.field} />
              </div>
              {selected.field.evidence_quote ? (
                <blockquote className="rounded border-l-2 border-emerald-600 bg-zinc-950 px-3 py-2 text-sm italic text-zinc-300">
                  “{selected.field.evidence_quote}”
                </blockquote>
              ) : (
                <p className="text-sm text-zinc-500">
                  {selected.field.status === "missing"
                    ? "论文中未找到该信息。"
                    : "无原文引文（推断值，请人工确认）。"}
                </p>
              )}
              <p className="text-xs text-zinc-500">
                {selected.field.source_page !== null && `页码：p.${selected.field.source_page} · `}
                {selected.field.source_section && `章节：${selected.field.source_section} · `}
                置信度：{(selected.field.confidence * 100).toFixed(0)}%
              </p>
              {selected.path && (
                <div className="flex gap-2">
                  <input
                    key={`${selected.path}-${cardVersion?.version}`}
                    ref={editInput}
                    defaultValue={selected.field.value === null ? "" : String(selected.field.value)}
                    placeholder="留空表示标记为缺失"
                    className="min-w-0 flex-1 rounded border border-zinc-700 bg-zinc-950 px-3 py-1.5 text-sm"
                  />
                  <button
                    onClick={saveEdit}
                    disabled={saving}
                    className="shrink-0 rounded bg-emerald-600 px-3 py-1.5 text-sm hover:bg-emerald-500 disabled:opacity-50"
                  >
                    {saving ? "保存中…" : "保存为新版本"}
                  </button>
                </div>
              )}
            </div>
          ) : (
            <p className="rounded-xl border border-dashed border-zinc-800 p-4 text-sm text-zinc-500">
              点击左侧字段查看原文证据；推断值可在此确认或修改。
            </p>
          )}
          {pdfUrl && (
            <iframe
              key={selected?.field.source_page ?? 0}
              src={`${pdfUrl}#page=${selected?.field.source_page ?? 1}`}
              title="论文 PDF"
              className="h-[600px] w-full rounded-xl border border-zinc-800 bg-white"
            />
          )}
        </section>
      </div>
    </main>
  );
}

export default function PaperClient({ paperId }: { paperId: string }) {
  return (
    <AuthGate>
      <Suspense fallback={<div className="p-6 text-zinc-500">加载中…</div>}>
        <PaperInner paperId={paperId} />
      </Suspense>
    </AuthGate>
  );
}