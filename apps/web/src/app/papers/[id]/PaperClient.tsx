"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import AuthGate from "@/components/AuthGate";
import Shell from "@/components/Shell";
import WorkflowInsights from "@/components/WorkflowInsights";
import WorkflowStepper from "@/components/WorkflowStepper";
import {
  IconArrowLeft,
  IconArrowRight,
  IconCheck,
  IconPdf,
  IconQuote,
  IconRefresh,
} from "@/components/icons";
import { apiFetch, confirmCard, getPaperWorkflow, resumeWorkflow, retryWorkflow } from "@/lib/api";
import { useWorkflow } from "@/lib/useWorkflow";
import type { CardVersion, CardVersionMeta, EvidenceField, PaperSummary } from "@/lib/types";
import { evidenceMeta, TONE_BADGE, WF_BUSY } from "@/lib/ui";

interface Selection {
  label: string;
  path: string | null;
  field: EvidenceField;
}

function StatusBadge({ field }: { field: EvidenceField }) {
  const meta = evidenceMeta(field);
  return <span className={`chip ${TONE_BADGE[meta.tone]}`}>{meta.label}</span>;
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
    <button onClick={onClick} className={`field-row ${active ? "field-row-active" : ""}`}>
      <span className="min-w-0">
        <span className="block text-[11px] text-fg-3">{label}</span>
        <span className="block truncate font-mono text-[13px] text-fg">
          {field.status === "missing" ? "—" : String(field.value)}
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-1.5">
        {field.source_page !== null && (
          <span className="font-mono text-[10.5px] text-fg-3">p.{field.source_page}</span>
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

  const busy = wfStatus != null && WF_BUSY.includes(wfStatus);
  const shownError = error ?? wfError;

  return (
    <main className="mx-auto w-full max-w-7xl space-y-4 px-5 py-6">
      <header className="rise flex flex-wrap items-center gap-3">
        <Link href="/papers" className="btn btn-ghost btn-sm px-2!" title="返回论文库">
          <IconArrowLeft className="h-4 w-4" />
        </Link>
        <div className="min-w-0 flex-1">
          <p className="label mb-0.5">Paper · Reproduction Card</p>
          <h1 className="display truncate text-xl font-bold">{paper?.title ?? "论文"}</h1>
        </div>
        {versions.length > 0 && (
          <select
            value={version ?? cardVersion?.version ?? ""}
            onChange={(e) => switchVersion(Number(e.target.value))}
            className="select w-44!"
            aria-label="Card 版本"
          >
            {versions.map((v) => (
              <option key={v.id} value={v.version}>
                v{v.version} · {v.origin === "extraction" ? "解析" : "用户修改"}
              </option>
            ))}
          </select>
        )}
        <button onClick={reparse} disabled={busy} className="btn btn-ghost btn-sm">
          <IconRefresh className="h-3.5 w-3.5" /> 重新解析
        </button>
        {wfStatus === "awaiting_spec_review" &&
          (cardVersion?.origin === "user_edit" ? (
            <button onClick={continueWorkflow} disabled={continuing} className="btn btn-info btn-sm">
              {continuing ? "提交中…" : "继续执行"}
            </button>
          ) : (
            <button
              onClick={confirmAndContinue}
              disabled={confirming || !card}
              className="btn btn-primary btn-sm"
            >
              <IconCheck className="h-3.5 w-3.5" />
              {confirming ? "提交中…" : "确认 Card 并继续"}
            </button>
          ))}
        {workflow?.experiment_id && (
          <Link
            href={`/experiments/${workflow.experiment_id}?workflow=${workflow.id}`}
            className="btn btn-primary btn-sm"
          >
            查看实验计划 <IconArrowRight className="h-3.5 w-3.5" />
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
        <p className="rise flex items-center gap-2 rounded-lg border border-info/40 bg-info/10 px-4 py-2 text-sm text-info">
          <span className="pulse-dot h-1.5 w-1.5 rounded-full bg-info" />
          工作流进行中…（{wfStatus}）
        </p>
      )}
      {wfStatus === "awaiting_spec_review" && (
        <p className="rise rounded-lg border border-warn/40 bg-warn/10 px-4 py-2 text-sm text-warn">
          请核对 Reproduction Card：推断值与缺失信息可在右侧确认或修改，然后点击「确认 Card 并继续」生成实验计划。
        </p>
      )}
      {wfStatus === "awaiting_plan_approval" && (
        <p className="rise rounded-lg border border-warn/40 bg-warn/10 px-4 py-2 text-sm text-warn">
          实验计划已生成，请前往实验页批准并执行。
        </p>
      )}
      {wfStatus === "completed" && (
        <p className="rise rounded-lg border border-ok/40 bg-ok/10 px-4 py-2 text-sm text-ok">
          复现工作流已完成，可在实验页查看训练指标与 Reproduction Report。
        </p>
      )}
      {shownError && (
        <p className="rise rounded-lg border border-bad/40 bg-bad/10 px-4 py-2 text-sm text-bad">
          {shownError}
        </p>
      )}

      <div className="grid gap-5 lg:grid-cols-2">
        <section className="rise rise-1 space-y-5">
          <p className="label flex items-center gap-2">
            <IconQuote className="h-3.5 w-3.5 text-brand" /> Extracted Values · 提取值
          </p>
          {rows.map((group) => (
            <div key={group.title} className="space-y-1.5">
              <h2 className="label flex items-center gap-2 text-fg-2!">
                {group.title}
                <span className="chip py-0!">{group.items.length}</span>
              </h2>
              {group.items.length === 0 && <p className="text-sm text-fg-3">（无）</p>}
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
            <div className="card space-y-2 border-warn/30 bg-warn/5 p-4">
              <h2 className="label text-warn!">缺失信息 Missing Information</h2>
              <ul className="list-inside list-disc space-y-1 text-[13px] text-fg-2">
                {card.missing_information.map((m) => (
                  <li key={m}>{m}</li>
                ))}
              </ul>
            </div>
          )}
          {!card && !busy && (
            <p className="rounded-xl border border-dashed border-line p-6 text-center text-sm text-fg-3">
              暂无 Reproduction Card，点击“重新解析”开始。
            </p>
          )}
        </section>

        <section className="rise rise-2 space-y-4">
          {selected ? (
            <div className="card space-y-3 p-4">
              <div className="flex items-center justify-between gap-2">
                <h3 className="display min-w-0 truncate text-sm font-semibold">{selected.label}</h3>
                <StatusBadge field={selected.field} />
              </div>
              {selected.field.evidence_quote ? (
                <blockquote className="quote">“{selected.field.evidence_quote}”</blockquote>
              ) : (
                <p className="text-sm text-fg-3">
                  {selected.field.status === "missing"
                    ? "论文中未找到该信息。"
                    : "无原文引文（推断值，请人工确认）。"}
                </p>
              )}
              <div className="space-y-1.5">
                <p className="font-mono text-[10.5px] text-fg-3">
                  {selected.field.source_page !== null && `p.${selected.field.source_page} · `}
                  {selected.field.source_section && `${selected.field.source_section} · `}
                  confidence {(selected.field.confidence * 100).toFixed(0)}%
                </p>
                <div className="meter">
                  <span style={{ width: `${Math.round(selected.field.confidence * 100)}%` }} />
                </div>
              </div>
              {selected.path && (
                <div className="flex gap-2">
                  <input
                    key={`${selected.path}-${cardVersion?.version}`}
                    ref={editInput}
                    defaultValue={selected.field.value === null ? "" : String(selected.field.value)}
                    placeholder="留空表示标记为缺失"
                    className="input min-w-0 flex-1"
                  />
                  <button onClick={saveEdit} disabled={saving} className="btn btn-primary btn-sm shrink-0">
                    {saving ? "保存中…" : "保存为新版本"}
                  </button>
                </div>
              )}
            </div>
          ) : (
            <p className="rounded-xl border border-dashed border-line p-4 text-sm text-fg-3">
              点击左侧字段查看原文证据；推断值可在此确认或修改。
            </p>
          )}

          {pdfUrl ? (
            <div className="terminal">
              <div className="terminal-bar">
                <span className="terminal-dot bg-bad/70" />
                <span className="terminal-dot bg-warn/70" />
                <span className="terminal-dot bg-ok/70" />
                <span className="label ml-2 flex items-center gap-1.5 text-[9.5px]!">
                  <IconPdf className="h-3 w-3" /> Document Preview
                </span>
                <span className="chip ml-auto py-0!">
                  p.{selected?.field.source_page ?? 1}
                </span>
              </div>
              <iframe
                key={selected?.field.source_page ?? 0}
                src={`${pdfUrl}#page=${selected?.field.source_page ?? 1}`}
                title="论文 PDF"
                className="h-[600px] w-full bg-white"
              />
            </div>
          ) : (
            <p className="rounded-xl border border-dashed border-line p-4 text-sm text-fg-3">
              PDF 预览不可用（未上传文件或签名链接过期）。
            </p>
          )}
        </section>
      </div>
    </main>
  );
}

export default function PaperClient({ paperId }: { paperId: string }) {
  return (
    <AuthGate>
      <Shell>
        <Suspense fallback={<div className="p-6 text-fg-3">加载中…</div>}>
          <PaperInner paperId={paperId} />
        </Suspense>
      </Shell>
    </AuthGate>
  );
}