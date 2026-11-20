"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import AuthGate from "@/components/AuthGate";
import DeletePaperButton from "@/components/DeletePaperButton";
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
import { apiFetch, confirmCard, confirmCardBatch, delegateCard, explainCard, getPaperWorkflow, resumeWorkflow, retryWorkflow } from "@/lib/api";
import { ConfirmationBadge, DelegationDialog, DelegationNotice, ExplanationCard, PreselectionHint, ReviewModeSelector, ReviewRequestStatus } from "@/components/ReviewControls";
import { hasDelegation, isReviewHandled, isRoutineCardPath, ordinaryReviewMode, useReviewMode, useReviewRequest, useReviewSelection } from "@/lib/useReview";
import { useWorkflow } from "@/lib/useWorkflow";
import type { CardVersion, CardVersionMeta, EvidenceField, PaperSummary } from "@/lib/types";
import { evidenceMeta, TONE_BADGE, WF_BUSY } from "@/lib/ui";

interface Selection {
  label: string;
  path: string | null;
  field: EvidenceField;
}

function StatusBadge({ field }: { field: EvidenceField }) {
  if (field.confirmed_by === "auto") return <ConfirmationBadge by="auto" />;
  const meta = evidenceMeta(field);
  return <span className="flex flex-wrap items-center gap-1">
    <span className={`chip ${TONE_BADGE[meta.tone]}`}>{meta.label}</span>
    <ConfirmationBadge by={field.confirmed_by} />
  </span>;
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
  const router = useRouter();
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
  const [delegationTarget, setDelegationTarget] = useState<CardVersion | null>(null);
  const [delegating, setDelegating] = useState(false);
  const [confirmedVersionId, setConfirmedVersionId] = useState<string | null>(null);
  const [continuing, setContinuing] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [loading, setLoading] = useState(true);
  const loadSequence = useRef(0);
  const editInput = useRef<HTMLInputElement>(null);
  const { workflowId, setWorkflowId, workflow, error: wfError, refresh } = useWorkflow(
    search.get("workflow")
  );
  const wfStatus = workflow?.status;
  const reviewKey = cardVersion ? `${paperId}:${cardVersion.id}` : "";
  const { mode, setMode } = useReviewMode(reviewKey, cardVersion?.card.review_mode);
  const normalMode = ordinaryReviewMode(mode);
  const storedDelegated = hasDelegation(cardVersion?.card);
  const delegated = storedDelegated && mode === "delegated";
  const canContinueSaved = delegated || (mode !== "delegated" && !!cardVersion &&
    confirmedVersionId === cardVersion.id && cardVersion.card.review_mode === mode);
  const busy = wfStatus != null && WF_BUSY.includes(wfStatus);
  const operationBusy = saving || confirming || delegating || continuing || retrying || loading || busy;
  const isLatest = cardVersion != null && cardVersion.version === Math.max(0, ...versions.map((v) => v.version));
  const writeDisabled = operationBusy || !isLatest;

  const loadVersions = useCallback(async () => {
    const list = await apiFetch<CardVersionMeta[]>(`/v1/papers/${paperId}/versions`);
    setVersions(list);
    return list;
  }, [paperId]);

  const loadCard = useCallback(
    async (v: number | null) => {
      const sequence = ++loadSequence.current;
      setLoading(true);
      try {
        const query = v === null ? "" : `?version=${v}`;
        const card = await apiFetch<CardVersion>(`/v1/papers/${paperId}/card${query}`);
        if (sequence !== loadSequence.current) return;
        setCardVersion(card);
        setVersion(v);
        setSelected(null);
      } finally {
        if (sequence === loadSequence.current) setLoading(false);
      }
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
        setLoading(false);
      })
      .catch((err) => { setLoading(false); setError(err.message); });
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
    if (operationBusy) return;
    setError(null);
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

  function showSavedVersion(next: CardVersion) {
    ++loadSequence.current;
    setLoading(false);
    setCardVersion(next);
    setSelected(null);
    setVersion(null);
    setVersions((previous) => previous.some((v) => v.id === next.id) ? previous : [next, ...previous]);
  }

  async function authorizeDelegation() {
    if (!delegationTarget || !cardVersion || writeDisabled || cardVersion.id !== delegationTarget.id) {
      throw new Error("版本已变化或暂不可授权，请取消后加载最新版本。");
    }
    setDelegating(true);
    setError(null);
    try {
      const next = await delegateCard(paperId, delegationTarget.version);
      showSavedVersion(next);
      setConfirmedVersionId(next.id);
      setDelegationTarget(null);
      // A refresh/resume failure must retain the saved version, not repeat authorization.
      try {
        await loadVersions();
      } catch (err) {
        setError(`托管版本 v${next.version} 已保存，但刷新失败：${err instanceof Error ? err.message : "请加载最新版本"}`);
      }
      if (workflowId && wfStatus === "awaiting_spec_review") await continueWorkflow();
    } finally {
      setDelegating(false);
    }
  }

  async function confirmAndContinue() {
    if (!workflowId || writeDisabled) return;
    if (canContinueSaved) {
      await continueWorkflow();
      return;
    }
    setConfirming(true);
    setError(null);
    try {
      const next = await confirmCard(paperId, normalMode);
      showSavedVersion(next);
      setConfirmedVersionId(next.id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "确认 Card 失败");
      setConfirming(false);
      return;
    }
    try {
      await loadVersions();
    } catch (err) {
      setError(`Card 已确认，但版本列表刷新失败：${err instanceof Error ? err.message : "请加载最新版本"}`);
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
      setError(`Card 版本已保存，继续工作流失败：${err instanceof Error ? err.message : "请重试"}。请点击「继续工作流」，无需重复授权或确认。`);
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
    if (!selected?.path || !cardVersion || operationBusy) return;
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
      const next = await apiFetch<CardVersion>(`/v1/papers/${paperId}/card/edits`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_version: cardVersion.version,
          review_mode: normalMode,
          edits: [{ field_path: selected.path, value }],
        }),
      });
      showSavedVersion(next);
      await loadVersions();
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

  const pendingFields = rows.flatMap((group) => group.items).filter((item): item is Selection & { path: string } =>
    item.path != null && !isReviewHandled(item.field, delegated) && (item.field.status === "inferred" || item.field.status === "missing"),
  );
  const versionNumber = cardVersion?.version;
  const requestExplanations = useCallback((signal: AbortSignal) => explainCard(paperId, versionNumber!, signal), [paperId, versionNumber]);
  const explanations = useReviewRequest(reviewKey, mode === "assisted" && pendingFields.length > 0 && !operationBusy && !delegationTarget, requestExplanations);
  const eligiblePaths = pendingFields.filter(({ path, field }) =>
    explanations.data?.preselected_paths.includes(path) && isRoutineCardPath(path) &&
    field.status === "inferred" && field.confidence >= explanations.data.selection_threshold,
  ).map((item) => item.path);
  const { selectedPaths, toggle } = useReviewSelection(reviewKey, eligiblePaths, eligiblePaths);
  const batchDisabled = writeDisabled || explanations.loading || !!explanations.error || !explanations.data || selectedPaths.length === 0;

  async function acceptHighConfidence() {
    if (!cardVersion || !explanations.data || batchDisabled) return;
    setSaving(true);
    setError(null);
    try {
      const next = await confirmCardBatch(paperId, cardVersion.version, selectedPaths, explanations.data.context_hash, normalMode);
      showSavedVersion(next);
      await loadVersions();
      await loadCard(null);
    } catch (err) {
      setError(`${err instanceof Error ? err.message : "批量确认失败"}。若版本已更新，请加载最新版本后重新获取解读。`);
    } finally {
      setSaving(false);
    }
  }

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
            disabled={operationBusy}
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
        <button onClick={reparse} disabled={operationBusy} className="btn btn-ghost btn-sm">
          <IconRefresh className="h-3.5 w-3.5" /> 重新解析
        </button>
        {paper && (
          <DeletePaperButton
            paper={paper}
            disabled={operationBusy}
            onDeleted={() => router.replace("/papers")}
          />
        )}
        {wfStatus === "awaiting_spec_review" && (
          <button onClick={confirmAndContinue} disabled={writeDisabled || !card} className="btn btn-primary btn-sm">
            <IconCheck className="h-3.5 w-3.5" />
            {confirming || continuing ? "提交中…" : canContinueSaved ? "继续工作流（已保存）" : "确认 Card 并继续"}
          </button>
        )}
        {workflow?.experiment_id && (
          <Link
            href={`/experiments/${workflow.experiment_id}?workflow=${workflow.id}`}
            className="btn btn-primary btn-sm"
          >
            查看实验计划 <IconArrowRight className="h-3.5 w-3.5" />
          </Link>
        )}
      </header>

      <ReviewModeSelector mode={mode} onChange={setMode} disabled={operationBusy || !cardVersion}
        delegationDisabled={writeDisabled} onDelegate={() => { if (!writeDisabled) setDelegationTarget(cardVersion); }} />
      {delegationTarget && <DelegationDialog version={delegationTarget.version} workflowDriven={workflowId != null}
        onConfirm={authorizeDelegation} onCancel={() => setDelegationTarget(null)}
        onExpert={() => { setDelegationTarget(null); setMode("expert"); }} />}
      {card?.review_mode === "delegated" && <DelegationNotice delegatedAt={card.delegated_at} delegatedBy={card.delegated_by} />}
      {storedDelegated && mode !== "delegated" && <p role="status" className="text-sm text-warn">
        当前普通模式尚未保存；原版本仍为托管版本，不代表已人工核验。人工编辑或确认保存后将撤销新版本授权。
      </p>}
      {cardVersion && !isLatest && <p className="text-sm text-warn">正在查看历史版本，人工编辑会创建新版本；授权、批量确认或继续工作流前请加载最新版本。</p>}
      {loading && <p role="status" className="text-xs text-fg-3">正在加载 Card 版本…</p>}
      <button className="btn btn-ghost btn-sm" disabled={operationBusy} onClick={() => {
        setError(null);
        Promise.all([loadVersions(), loadCard(null)]).catch((err) => setError(err.message));
      }}>加载最新版本</button>

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
          {delegated ? "Card 已托管授权，可继续工作流；系统将自动生成建议、接受、批准并按当前预算执行，无需后续逐项点击。"
            : "请核对 Reproduction Card：推断值与缺失信息可在右侧确认或修改，然后点击「确认 Card 并继续」生成实验计划。"}
        </p>
      )}
      {wfStatus === "awaiting_plan_approval" && (
        <p className="rise rounded-lg border border-warn/40 bg-warn/10 px-4 py-2 text-sm text-warn">
          {storedDelegated ? "托管工作流正在处理实验计划，无需逐项点击；可前往实验页查看授权及执行状态。" : "实验计划已生成，请前往实验页批准并执行。"}
        </p>
      )}
      {wfStatus === "completed" && (
        <p className="rise rounded-lg border border-ok/40 bg-ok/10 px-4 py-2 text-sm text-ok">
          复现工作流已完成，可在实验页查看训练指标与 Reproduction Report。
        </p>
      )}
      {shownError && (
        <div role="alert" className="rise space-y-2 rounded-lg border border-bad/40 bg-bad/10 px-4 py-2 text-sm text-bad">
          <p>{shownError}</p>
          {storedDelegated && <button className="btn btn-ghost btn-sm" disabled={operationBusy} onClick={() => setMode("expert")}>切换专家模式，人工审核</button>}
        </div>
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
          {card && (
            <div className="card space-y-3 p-4">
              <h2 className="label text-info!">待审核字段 · AI 辅助（{pendingFields.length}）</h2>
              <ReviewRequestStatus label="通俗解读" loading={explanations.loading} error={explanations.error}
                loaded={!!explanations.data} disabled={operationBusy || pendingFields.length === 0}
                onLoad={explanations.load} onExpert={() => setMode("expert")} />
              {explanations.data && <>
                <PreselectionHint threshold={explanations.data.selection_threshold} />
                <button className="btn btn-info btn-sm" disabled={batchDisabled} onClick={acceptHighConfidence}>
                  {saving ? "保存中…" : `接受已选高置信项（${selectedPaths.length}）`}
                </button>
                <p className="text-xs text-fg-3">仅确认所选常规推断项，不确认整张 Card，也不继续工作流。</p>
              </>}
              {pendingFields.length === 0 && <p className="text-xs text-fg-3">本版本没有待审核的推断或缺失字段。</p>}
              {pendingFields.map((item) => {
                const explanation = explanations.data?.explanations.find((e) => e.field_path === item.path);
                const routine = isRoutineCardPath(item.path);
                return (
                  <div key={`${reviewKey}:${item.path}`} className={`space-y-2 rounded-lg border border-line border-l-2 p-3 ${routine ? "border-l-warn" : "border-l-bad"}`}>
                    <div className="flex flex-wrap items-center gap-2">
                      <span className={`chip ${routine ? "badge-warn" : "badge-bad"}`}>{routine ? "常规" : "关键"}</span>
                      <button className="text-left text-xs text-info underline underline-offset-2" onClick={() => setSelected(item)}>{item.label} · {item.path}</button>
                    </div>
                    <p className="break-words font-mono text-xs text-fg-2">当前值：{item.field.value == null ? "—（缺失）" : String(item.field.value)} · 置信度 {(item.field.confidence * 100).toFixed(0)}%</p>
                    {eligiblePaths.includes(item.path) ? (
                      <label className="flex items-center gap-2 text-xs text-fg-2">
                        <input type="checkbox" checked={selectedPaths.includes(item.path)}
                          disabled={writeDisabled || explanations.loading || !!explanations.error}
                          onChange={(event) => toggle(item.path, event.target.checked)}
                          className="h-4 w-4 accent-[var(--color-brand)]" />
                        建议接受此高置信推断项
                      </label>
                    ) : <p className="text-xs text-fg-3">需人工核对并编辑或确认，不参与高置信批量确认。</p>}
                    {explanation && <ExplanationCard explanation={explanation} mode={mode} />}
                  </div>
                );
              })}
            </div>
          )}
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
          {!!card?.review_decisions?.length && (
            <details className="card space-y-2 p-4">
              <summary className="cursor-pointer text-sm text-fg focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand">本版本审核记录（{card.review_decisions.length}）</summary>
              {card.review_decisions.map((decision) => (
                <div key={decision.id} className="space-y-1 border-t border-line pt-2 text-xs text-fg-2">
                  <p className="break-words font-mono">{decision.field_path}: {String(decision.before)} → {String(decision.after)}</p>
                  <ConfirmationBadge by={decision.confirmed_by} />
                  <p className="break-words">{decision.review_mode === "delegated" ? "托管模式" : decision.review_mode === "expert" ? "专家模式" : decision.review_mode === "assisted" ? "辅助模式" : "审核模式未记录"}
                    {decision.actor && ` · 操作者 ${decision.actor}`}{decision.timestamp && ` · ${decision.timestamp}`}</p>
                  {decision.recommendation_reasoning && <p className="break-words">推荐理由：{decision.recommendation_reasoning}</p>}
                  {decision.recommendation_risk && <p className="break-words text-warn">风险：{decision.recommendation_risk}</p>}
                </div>
              ))}
            </details>
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
                    disabled={operationBusy}
                    aria-label={`${selected.label}字段值`}
                    defaultValue={selected.field.value === null ? "" : String(selected.field.value)}
                    placeholder="留空表示标记为缺失"
                    className="input min-w-0 flex-1"
                  />
                  <button onClick={saveEdit} disabled={operationBusy} className="btn btn-primary btn-sm shrink-0">
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
          <PaperInner key={paperId} paperId={paperId} />
        </Suspense>
      </Shell>
    </AuthGate>
  );
}