"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import AuthGate from "@/components/AuthGate";
import Shell from "@/components/Shell";
import WorkflowInsights from "@/components/WorkflowInsights";
import WorkflowStepper from "@/components/WorkflowStepper";
import { IconArrowLeft, IconCheck, IconWarn } from "@/components/icons";
import RunPanel from "./RunPanel";
import { adoptPlanRecommendations, apiFetch, delegatePlan, explainPlan, overridePlanTier, recommendPlan, resumeWorkflow, retryWorkflow } from "@/lib/api";
import { ConfirmationBadge, DelegationDialog, DelegationNotice, ExplanationCard, PreselectionHint, RecommendationCard, ReviewModeSelector, ReviewRequestStatus } from "@/components/ReviewControls";
import { hasDelegation, isReviewHandled, ordinaryReviewMode, useReviewMode, useReviewRequest, useReviewSelection } from "@/lib/useReview";
import { useWorkflow } from "@/lib/useWorkflow";
import type {
  Assumption,
  ConfirmationSource,
  FieldExplanation,
  FieldRecommendation,
  ReviewMode,
  ExperimentDetail,
  ExperimentPlan,
  PlanParam,
  PlanVersion,
  PlanVersionMeta,
  RiskTier,
} from "@/lib/types";
import { RISK_META, SOURCE_META, TONE_BADGE, tierOf } from "@/lib/ui";

const KIND_LABEL: Record<string, string> = {
  inferred: "推断",
  missing_proposal: "缺失·提案",
  mvp_scaling: "MVP缩放",
  nonstandard_choice: "非标准·替代",
};

const ORIGIN_LABEL: Record<string, string> = {
  generated: "生成",
  user_edit: "用户修改",
  oom_retry: "OOM 降批",
};

function SourceBadge({ source, confirmed, by }: { source: string; confirmed?: boolean; by?: ConfirmationSource | null }) {
  if (by === "ai_assisted" || by === "auto") return <ConfirmationBadge by={by} />;
  const meta = SOURCE_META[source as keyof typeof SOURCE_META];
  const tone = meta?.tone ?? "mute";
  return (
    <span className={`chip ${TONE_BADGE[tone]}`}>
      {meta?.label ?? source}
      {confirmed && source !== "human_confirmed" ? "·已确认" : ""}
    </span>
  );
}

interface Editable {
  path: string;
  label: string;
  value: string | number | boolean | null;
  param?: PlanParam;
}

function buildEditables(plan: ExperimentPlan): Editable[] {
  const t = plan.training_config;
  const list: Editable[] = [
    { path: "training_config.optimizer", label: "优化器 Optimizer", value: t.optimizer.value, param: t.optimizer },
    { path: "training_config.learning_rate", label: "学习率 Learning Rate", value: t.learning_rate.value, param: t.learning_rate },
    { path: "training_config.loss_function", label: "损失函数 Loss", value: t.loss_function.value, param: t.loss_function },
    { path: "training_config.batch_size", label: "批大小 Batch Size", value: t.batch_size.value, param: t.batch_size },
    { path: "training_config.epochs", label: "Epochs", value: t.epochs.value, param: t.epochs },
  ];
  if (t.weight_decay) {
    list.push({ path: "training_config.weight_decay", label: "Weight Decay", value: t.weight_decay.value, param: t.weight_decay });
  }
  list.push(
    { path: "dataset_config.train_subset_size", label: "训练子集大小", value: plan.dataset_config.train_subset_size },
    { path: "dataset_config.test_subset_size", label: "测试子集大小", value: plan.dataset_config.test_subset_size },
    { path: "runtime_budget.max_seconds", label: "时间预算 (秒)", value: plan.runtime_budget.max_seconds },
    { path: "evaluation_config.tolerance", label: "对比容差", value: plan.evaluation_config.tolerance },
  );
  return list;
}

function EditRow({ item, active, onClick, delegated }: { item: Editable; active: boolean; onClick: () => void; delegated: boolean }) {
  const p = item.param;
  return (
    <button onClick={onClick} className={`field-row ${active ? "field-row-active" : ""}`}>
      <span className="min-w-0">
        <span className="block text-[11px] text-fg-3">{item.label}</span>
        <span className="block truncate font-mono text-[13px] text-fg">
          {item.value === null ? "—" : String(item.value)}
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-1.5">
        {p?.source === "mvp_scaling" && p.paper_value != null && (
          <span className="font-mono text-[10.5px] text-fg-3">原文 {String(p.paper_value)}</span>
        )}
        {p && <SourceBadge source={p.source} confirmed={p.human_confirmed} by={p.confirmed_by} />}
        {p?.requires_confirmation && !isReviewHandled(p, delegated) && (
          <IconWarn className="h-3.5 w-3.5 text-warn" />
        )}
      </span>
    </button>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <h2 className="label text-fg-2!">{title}</h2>
      {children}
    </div>
  );
}

const TIER_BORDER: Record<RiskTier, string> = {
  critical: "border-l-bad",
  routine: "border-l-warn",
  info: "border-l-line",
};

function AssumptionCard({
  a,
  saving,
  onConfirm,
  explanation,
  recommendation,
  mode,
  checked,
  adviceDisabled,
  delegated,
  onCheck,
  onAdopt,
  onModify,
  onTierChange,
  tierDisabled,
}: {
  a: Assumption;
  saving: boolean;
  onConfirm: (a: Assumption) => void;
  explanation?: FieldExplanation;
  recommendation?: FieldRecommendation;
  mode: ReviewMode;
  checked: boolean;
  adviceDisabled: boolean;
  delegated: boolean;
  onCheck: (checked: boolean) => void;
  onAdopt: () => void;
  onModify: () => void;
  onTierChange: (tier: RiskTier) => void;
  tierDisabled: boolean;
}) {
  const tier = tierOf(a);
  const meta = RISK_META[tier];
  const pending = a.requires_confirmation && !isReviewHandled(a, delegated);
  return (
    <div className={`card space-y-1.5 border-l-2 p-3 text-sm ${TIER_BORDER[tier]}`}>
      <div className="flex items-center justify-between gap-2">
        <span className="truncate font-mono text-[10.5px] text-fg-3">{a.field_path}</span>
        <span className="flex shrink-0 items-center gap-1.5">
          <select
            className={`select w-auto! py-0.5! pl-2! pr-6! text-[10.5px] ${TONE_BADGE[meta.tone]}`}
            value={tier}
            disabled={tierDisabled}
            aria-label={`调整 ${a.field_path} 的风险分级`}
            title={`${meta.hint}。人工调整将生成新版本并留痕。`}
            onChange={(event) => onTierChange(event.target.value as RiskTier)}
          >
            <option value="critical">关键</option>
            <option value="routine">常规</option>
            <option value="info">说明</option>
          </select>
          <span className="chip">{KIND_LABEL[a.kind] ?? a.kind}</span>
        </span>
      </div>
      <p className="text-[13px] leading-relaxed text-fg-2">{a.rationale}</p>
      <p className="font-mono text-[10.5px] text-fg-3">提案值：{String(a.value)}</p>
      {a.evidence_quote && (
        <blockquote className="quote">
          “{a.evidence_quote}”
          {a.source_page != null && (
            <span className="ml-2 font-mono text-[10.5px] not-italic text-fg-3">p.{a.source_page}</span>
          )}
        </blockquote>
      )}
      {explanation && <ExplanationCard explanation={explanation} mode={mode} />}
      {pending && recommendation && (
        <RecommendationCard recommendation={recommendation} currentValue={a.value} checked={checked}
          disabled={adviceDisabled} onCheck={onCheck} onAdopt={onAdopt} onModify={onModify} />
      )}
      <ConfirmationBadge by={a.confirmed_by} />
      {pending ? (
        <button onClick={() => onConfirm(a)} disabled={saving} className="btn btn-info btn-sm">
          <IconCheck className="h-3 w-3" /> 确认此假设
        </button>
      ) : (
        <span className={`text-xs ${a.human_confirmed ? "text-ok" : "text-fg-3"}`}>
          {a.confirmed_by === "auto" ? "已自动处理，未经人工逐项核验" : a.human_confirmed ? "已人工确认" : "无需确认（MVP 缩放/说明）"}
        </span>
      )}
    </div>
  );
}

function PlanInner({ experimentId }: { experimentId: string }) {
  const search = useSearchParams();
  const [experiment, setExperiment] = useState<ExperimentDetail | null>(null);
  const [versions, setVersions] = useState<PlanVersionMeta[]>([]);
  const [current, setCurrent] = useState<PlanVersion | null>(null);
  const [selected, setSelected] = useState<Editable | null>(null);
  const [editPrefill, setEditPrefill] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [approving, setApproving] = useState(false);
  const [delegationTarget, setDelegationTarget] = useState<PlanVersion | null>(null);
  const [delegating, setDelegating] = useState(false);
  const [continuing, setContinuing] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [loading, setLoading] = useState(true);
  const loadSequence = useRef(0);
  const valueInput = useRef<HTMLInputElement>(null);
  const reasonInput = useRef<HTMLInputElement>(null);
  const confirmCheck = useRef<HTMLInputElement>(null);
  const { workflowId, workflow, error: wfError, refresh } = useWorkflow(search.get("workflow"));
  const wfStatus = workflow?.status;
  const workflowDriven = workflowId != null;
  const reviewKey = current ? `${experimentId}:${current.id}` : "";
  const { mode, setMode } = useReviewMode(reviewKey, current?.plan.review_mode);
  const normalMode = ordinaryReviewMode(mode);
  const storedDelegated = hasDelegation(current?.plan);
  const delegated = storedDelegated && mode === "delegated";
  const reviewingDelegatedNormally = current?.plan.review_mode === "delegated" && mode !== "delegated";
  const versionNumber = current?.version;
  const operationBusy = saving || approving || delegating || continuing || retrying || loading;
  const latestVersion = Math.max(experiment?.plan?.version ?? 0, ...versions.map((v) => v.version));
  const isLatest = current != null && current.version === latestVersion;
  const writeDisabled = operationBusy || !isLatest;
  const hasPending = current?.plan.assumptions.some((a) => a.requires_confirmation && !isReviewHandled(a, delegated) && tierOf(a) !== "info") ?? false;
  const requestExplanations = useCallback((signal: AbortSignal) => explainPlan(experimentId, versionNumber!, signal), [experimentId, versionNumber]);
  const requestRecommendations = useCallback((signal: AbortSignal) => recommendPlan(experimentId, versionNumber!, signal), [experimentId, versionNumber]);
  const autoReview = mode === "assisted" && hasPending && !operationBusy && !delegationTarget;
  const explanations = useReviewRequest(reviewKey, autoReview, requestExplanations);
  const advice = useReviewRequest(reviewKey, autoReview, requestRecommendations);
  const recommendations = (advice.data?.recommendations ?? []).filter((r) =>
    current?.plan.assumptions.some((a) => a.field_path === r.field_path && a.requires_confirmation && !isReviewHandled(a, delegated) && tierOf(a) !== "info"),
  );
  const preselected = recommendations.filter((r) => {
    const a = current?.plan.assumptions.find((item) => item.field_path === r.field_path);
    const threshold = advice.data?.selection_threshold ?? 0.9;
    return advice.data?.preselected_paths.includes(r.field_path) && a && tierOf(a) === "routine" &&
      a.auto_acceptable && a.kind === "inferred" && (a.confidence ?? 0) >= threshold &&
      r.action === "accept" && r.confidence >= threshold;
  }).map((r) => r.field_path);
  const { selectedPaths, toggle } = useReviewSelection(reviewKey, recommendations.map((r) => r.field_path), preselected);
  const adviceDisabled = writeDisabled || advice.loading || !!advice.error;

  const loadVersions = useCallback(async () => {
    const list = await apiFetch<PlanVersionMeta[]>(`/v1/experiments/${experimentId}/plans`);
    setVersions(list);
  }, [experimentId]);

  const loadExperiment = useCallback(async (preserveCurrent = false) => {
    const sequence = ++loadSequence.current;
    setLoading(true);
    try {
      const exp = await apiFetch<ExperimentDetail>(`/v1/experiments/${experimentId}`);
      if (sequence !== loadSequence.current) return;
      setExperiment(exp);
      if (!preserveCurrent) {
        setCurrent(exp.plan);
        setSelected(null);
        setEditPrefill(null);
      }
    } finally {
      if (sequence === loadSequence.current) setLoading(false);
    }
  }, [experimentId]);

  useEffect(() => {
    loadExperiment().catch((err) => setError(err.message));
    loadVersions().catch(() => {});
  }, [loadExperiment, loadVersions]);

  useEffect(() => {
    if (!wfStatus || wfStatus === "planning") return;
    loadExperiment().catch(() => {});
    loadVersions().catch(() => {});
  }, [wfStatus, loadExperiment, loadVersions]);

  async function switchVersion(v: number) {
    if (operationBusy) return;
    const sequence = ++loadSequence.current;
    setLoading(true);
    setError(null);
    try {
      const pv = await apiFetch<PlanVersion>(`/v1/experiments/${experimentId}/plans/${v}`);
      if (sequence !== loadSequence.current) return;
      setCurrent(pv);
      setSelected(null);
      setEditPrefill(null);
    } catch (err) {
      if (sequence === loadSequence.current) setError(err instanceof Error ? err.message : "加载版本失败");
    } finally {
      if (sequence === loadSequence.current) setLoading(false);
    }
  }

  function showSavedVersion(next: PlanVersion) {
    // A completed write supersedes any older workflow-triggered read.
    ++loadSequence.current;
    setLoading(false);
    setCurrent(next);
    setSelected(null);
    setEditPrefill(null);
    setExperiment((previous) => previous ? { ...previous, plan: next } : previous);
    setVersions((previous) => previous.some((v) => v.id === next.id) ? previous : [next, ...previous]);
  }

  async function authorizeDelegation() {
    if (!delegationTarget || !current || writeDisabled || current.id !== delegationTarget.id) {
      throw new Error("版本已变化或暂不可授权，请取消后加载最新版本。");
    }
    setDelegating(true);
    setError(null);
    try {
      // Only this endpoint grants consent. Later failures must never retry the write.
      const next = await delegatePlan(experimentId, delegationTarget.version);
      showSavedVersion(next);
      setDelegationTarget(null);
      try {
        await Promise.all([loadVersions(), loadExperiment(true)]);
      } catch (err) {
        setError(`托管版本 v${next.version} 已保存，但刷新失败：${err instanceof Error ? err.message : "请加载最新版本"}`);
      }
      if (workflowId && wfStatus === "awaiting_plan_approval") await continueWorkflow();
    } finally {
      setDelegating(false);
    }
  }

  async function persistEdit(
    path: string,
    value: string | number | boolean | null,
    humanConfirmed: boolean,
    reason: string,
  ) {
    if (!current || operationBusy) return;
    setSaving(true);
    setError(null);
    try {
      const next = await apiFetch<PlanVersion>(`/v1/experiments/${experimentId}/plans`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_version: current.version,
          review_mode: normalMode,
          changes: [{ field_path: path, value, human_confirmed: humanConfirmed }],
          change_reason: reason,
          origin: "user_edit",
        }),
      });
      showSavedVersion(next);
      await loadVersions();
      await loadExperiment();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  function saveSelected() {
    if (!selected) return;
    const raw = valueInput.current?.value ?? "";
    let value: string | number | boolean | null;
    if (raw.trim() === "") value = null;
    else if (typeof selected.value === "number") {
      const n = Number(raw);
      if (!Number.isFinite(n)) {
        setError("请输入有效数字");
        return;
      }
      value = n;
    } else if (typeof selected.value === "boolean") value = raw === "true";
    else value = raw;
    const reason = reasonInput.current?.value.trim() || "用户修改计划参数";
    persistEdit(selected.path, value, confirmCheck.current?.checked ?? (mode === "expert"), reason);
  }

  function confirmAssumption(a: Assumption) {
    persistEdit(
      a.field_path,
      a.value as string | number | boolean | null,
      true,
      `确认假设：${a.field_path}`,
    );
  }

  async function changeTier(a: Assumption, tier: RiskTier) {
    if (!current || operationBusy || tier === tierOf(a)) return;
    setSaving(true);
    setError(null);
    try {
      const next = await overridePlanTier(experimentId, current.version, a.field_path, tier);
      showSavedVersion(next);
      await loadVersions();
      await loadExperiment();
    } catch (err) {
      setError(err instanceof Error ? err.message : "调整分级失败");
    } finally {
      setSaving(false);
    }
  }

  function openModify(r: FieldRecommendation) {
    const item = editables.find((e) => e.path === r.field_path);
    if (!item) return;
    setSelected(item);
    setEditPrefill(r.suggested_value == null ? "" : String(r.suggested_value));
  }

  async function acceptRoutine() {
    const p = current?.plan;
    if (!current || !p || operationBusy) return;
    const targets = p.assumptions.filter(
      (a) =>
        tierOf(a) === "routine" &&
        a.requires_confirmation &&
        !isReviewHandled(a, delegated) &&
        a.auto_acceptable,
    );
    if (targets.length === 0) return;
    setSaving(true);
    setError(null);
    try {
      const next = await apiFetch<PlanVersion>(`/v1/experiments/${experimentId}/plans`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_version: current.version,
          review_mode: normalMode,
          changes: targets.map((a) => ({
            field_path: a.field_path,
            value: a.value,
            human_confirmed: true,
          })),
          change_reason: `批量采纳 ${targets.length} 项常规假设`,
          origin: "user_edit",
        }),
      });
      showSavedVersion(next);
      await loadVersions();
      await loadExperiment();
    } catch (err) {
      setError(err instanceof Error ? err.message : "批量采纳失败");
    } finally {
      setSaving(false);
    }
  }

  async function adoptRecommendations(targets: FieldRecommendation[]) {
    if (!current || !advice.data || adviceDisabled || targets.length === 0) return;
    setSaving(true);
    setError(null);
    try {
      const next = await adoptPlanRecommendations(experimentId, current.version, targets.map((r) => r.id), advice.data.context_hash, normalMode);
      showSavedVersion(next);
      await loadVersions();
      await loadExperiment();
    } catch (err) {
      setError(`${err instanceof Error ? err.message : "采纳失败"}。若版本已更新，请加载最新版本后重新获取推荐。`);
    } finally {
      setSaving(false);
    }
  }

  async function approve() {
    if (!current || writeDisabled) return;
    setApproving(true);
    setError(null);
    try {
      const next = await apiFetch<PlanVersion>(
        `/v1/experiments/${experimentId}/plans/${current.version}/approve`,
        { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ review_mode: normalMode }) },
      );
      showSavedVersion(next);
      await loadExperiment();
      await loadVersions();
    } catch (err) {
      setError(err instanceof Error ? err.message : "审批失败");
      setApproving(false);
      return;
    }
    setApproving(false);
    if (workflowDriven && workflowId && wfStatus === "awaiting_plan_approval") {
      await continueWorkflow();
    }
  }

  async function continueWorkflow() {
    if (!workflowId) return;
    setContinuing(true);
    setError(null);
    try {
      await resumeWorkflow(workflowId, "plan");
      await refresh();
    } catch (err) {
      setError(`计划版本已保存，继续执行失败：${err instanceof Error ? err.message : "请重试"}。请点击「继续执行」，无需重复授权或批准。`);
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

  const plan = current?.plan;
  const editables = plan ? buildEditables(plan) : [];
  const assumptions = plan?.assumptions ?? [];
  const isPending = (a: Assumption) => a.requires_confirmation && !isReviewHandled(a, delegated);
  const criticalPending = assumptions.filter((a) => isPending(a) && tierOf(a) === "critical");
  const routinePending = assumptions.filter(
    (a) => isPending(a) && tierOf(a) === "routine" && a.auto_acceptable,
  );
  const groups: Record<RiskTier, Assumption[]> = {
    critical: assumptions.filter((a) => tierOf(a) === "critical"),
    routine: assumptions.filter((a) => tierOf(a) === "routine"),
    info: assumptions.filter((a) => tierOf(a) === "info"),
  };
  const decisionItems = assumptions.filter((a) => tierOf(a) !== "info");
  const handledCount = decisionItems.filter((a) => isReviewHandled(a, delegated)).length;
  const progressPct = decisionItems.length === 0 ? 100 : Math.round((handledCount / decisionItems.length) * 100);
  const savedApproved = current?.approved_at != null;
  // An existing delegated approval must not masquerade as an expert/assisted approval.
  const approved = savedApproved && !reviewingDelegatedNormally &&
    (current?.plan.review_mode !== "delegated" || storedDelegated);
  const canApprove = (criticalPending.length === 0 || reviewingDelegatedNormally) &&
    !approved && !!current && !writeDisabled && mode !== "delegated";
  const shownError = error ?? wfError;
  const criticalRecommendations = recommendations.filter((r) => groups.critical.some((a) => a.field_path === r.field_path)).length;
  function renderAssumption(a: Assumption) {
    const recommendation = recommendations.find((r) => r.field_path === a.field_path);
    return <AssumptionCard key={`${reviewKey}:${a.field_path}`} a={a} delegated={delegated} saving={operationBusy} onConfirm={confirmAssumption}
      mode={mode} explanation={explanations.data?.explanations.find((e) => e.field_path === a.field_path)}
      recommendation={recommendation} checked={selectedPaths.includes(a.field_path)} adviceDisabled={adviceDisabled}
      onCheck={(checked) => toggle(a.field_path, checked)}
      onAdopt={() => recommendation && adoptRecommendations([recommendation])}
      onModify={() => recommendation && openModify(recommendation)}
      onTierChange={(tier) => changeTier(a, tier)}
      tierDisabled={operationBusy} />;
  }

  return (
    <main className="mx-auto w-full max-w-7xl space-y-4 px-5 py-6">
      <header className="rise flex flex-wrap items-center gap-3">
        <Link
          href={
            experiment
              ? `/papers/${experiment.paper_id}${workflowDriven ? `?workflow=${workflowId}` : ""}`
              : "/papers"
          }
          className="btn btn-ghost btn-sm px-2!"
          title="返回论文"
        >
          <IconArrowLeft className="h-4 w-4" />
        </Link>
        <div className="min-w-0 flex-1">
          <p className="label mb-0.5">Experiment · Plan</p>
          <h1 className="display truncate text-xl font-bold">
            {experiment?.name ?? "实验计划"}
          </h1>
        </div>
        {experiment && <span className="chip">{experiment.status}</span>}
        {versions.length > 0 && (
          <select
            value={current?.version ?? ""}
            disabled={operationBusy}
            onChange={(e) => switchVersion(Number(e.target.value))}
            className="select w-48!"
            aria-label="计划版本"
          >
            {versions.map((v) => (
              <option key={v.id} value={v.version}>
                v{v.version} · {ORIGIN_LABEL[v.origin] ?? v.origin}
                {v.approved_at ? " · 已批准" : ""}
              </option>
            ))}
          </select>
        )}
        <button
          onClick={approve}
          disabled={!canApprove || approving}
          className="btn btn-primary btn-sm"
        >
          <IconCheck className="h-3.5 w-3.5" />
          {approving
            ? "提交中…"
            : reviewingDelegatedNormally
              ? `按${mode === "expert" ? "专家" : "辅助"}模式重新校验并批准`
              : approved
                ? storedDelegated ? "已托管批准 · 未人工核验" : "已批准"
                : workflowDriven && wfStatus === "awaiting_plan_approval"
                  ? "批准并执行"
                  : "批准计划"}
        </button>
        {workflowDriven && wfStatus === "awaiting_plan_approval" && approved && (
          <button onClick={continueWorkflow} disabled={operationBusy || !isLatest} className="btn btn-info btn-sm">
            {continuing ? "提交中…" : "继续执行"}
          </button>
        )}
      </header>

      <ReviewModeSelector mode={mode} onChange={setMode} disabled={operationBusy || !current}
        delegationDisabled={writeDisabled} onDelegate={() => { if (!writeDisabled) setDelegationTarget(current); }} />
      {delegationTarget && <DelegationDialog version={delegationTarget.version} workflowDriven={workflowDriven}
        budget={`${delegationTarget.plan.runtime_budget.max_seconds} 秒 · ${delegationTarget.plan.runtime_budget.device}`}
        onConfirm={authorizeDelegation} onCancel={() => setDelegationTarget(null)}
        onExpert={() => { setDelegationTarget(null); setMode("expert"); }} />}
      {plan?.review_mode === "delegated" && <DelegationNotice delegatedAt={plan.delegated_at} delegatedBy={plan.delegated_by} />}
      {reviewingDelegatedNormally && <p role="status" className="rounded-lg border border-warn/40 bg-warn/10 px-4 py-3 text-sm text-warn">
        仅切换了本地审核模式，尚未保存。原版本{savedApproved ? `已于 ${current?.approved_at} 按托管批准` : "的托管记录保留"}，不代表专家或辅助模式已批准。
        按所选模式重新批准将校验关键假设并撤销新版本授权；也可人工编辑创建新版本。
      </p>}
      {current && !isLatest && <p className="text-sm text-warn">正在查看历史版本，仍可人工编辑产生分支；授权、AI 采纳或批准前请加载最新版本。</p>}
      {loading && <p role="status" className="text-xs text-fg-3">正在加载计划版本…</p>}
      <button className="btn btn-ghost btn-sm" disabled={operationBusy} onClick={() => {
        setError(null);
        Promise.all([loadExperiment(), loadVersions()]).catch((err) => setError(err.message));
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

      {approved && (
        <p className={`rise rounded-lg border px-4 py-2 text-sm ${storedDelegated ? "border-warn/40 bg-warn/10 text-warn" : "border-ok/40 bg-ok/10 text-ok"}`}>
          该计划版本已{storedDelegated ? "托管" : ""}批准（{current?.approved_at}），{storedDelegated ? "关键假设未经人工逐项核验，结果仅供趋势参考。" : "可进入代码生成阶段。"}
        </p>
      )}
      {!approved && criticalPending.length > 0 && (
        <p className="rise flex flex-wrap items-center gap-2 rounded-lg border border-bad/40 bg-bad/10 px-4 py-2 text-sm text-bad">
          <IconWarn className="h-4 w-4 shrink-0" />
          还需确认 {criticalPending.length} 项关键假设后方可批准：
          <span className="font-mono text-xs">{criticalPending.map((a) => a.field_path).join("、")}</span>
        </p>
      )}
      {shownError && (
        <div role="alert" className="rise space-y-2 rounded-lg border border-bad/40 bg-bad/10 px-4 py-2 text-sm text-bad">
          <p>{shownError}</p>
          {storedDelegated && <button className="btn btn-ghost btn-sm" disabled={operationBusy} onClick={() => setMode("expert")}>切换专家模式，人工审核</button>}
        </div>
      )}

      {plan && (
        <div className="grid gap-5 lg:grid-cols-2">
          <section className="rise rise-1 space-y-5">
            <div className="card border-scale/30 bg-scale/8 p-4">
              <span className="chip badge-scale mb-2">{plan.scaling_profile}</span>
              <p className="text-[13px] leading-relaxed text-fg-2">{plan.rationale}</p>
            </div>

            <Section title="训练配置 Training（点击编辑/确认）">
              {editables
                .filter((e) => e.path.startsWith("training_config."))
                .map((item) => (
                  <EditRow
                    key={item.path}
                    item={item}
                    delegated={delegated}
                    active={selected?.path === item.path}
                    onClick={() => { setEditPrefill(null); setSelected(item); }}
                  />
                ))}
              <p className="font-mono text-[10.5px] text-fg-3">
                seed = {plan.training_config.seed}（固定）
              </p>
            </Section>

            <Section title="数据集 Dataset">
              {editables
                .filter((e) => e.path.startsWith("dataset_config."))
                .map((item) => (
                  <EditRow
                    key={item.path}
                    item={item}
                    delegated={delegated}
                    active={selected?.path === item.path}
                    onClick={() => { setEditPrefill(null); setSelected(item); }}
                  />
                ))}
              <p className="font-mono text-[10.5px] text-fg-3">
                {plan.dataset_config.name} · input {plan.dataset_config.input_shape.join("×")} ·{" "}
                {plan.dataset_config.num_classes} classes · full train{" "}
                {plan.dataset_config.full_train_size ?? "—"} / test{" "}
                {plan.dataset_config.full_test_size ?? "—"}
              </p>
            </Section>

            <Section title="模型 Model">
              <div className="card px-3 py-2 text-sm">
                <span className="font-mono text-[13px] text-fg">{plan.model_config.architecture}</span>
                {plan.model_config.description && (
                  <span className="mt-0.5 block text-xs text-fg-3">{plan.model_config.description}</span>
                )}
              </div>
            </Section>

            <Section title="预处理 Preprocessing">
              {plan.preprocessing_config.steps.map((s) => (
                <div key={s.name} className="card px-3 py-2 text-sm">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-fg">{s.name}</span>
                    <SourceBadge source={s.source} by={assumptions.find((a) => a.field_path === `preprocessing_config.${s.name}`)?.confirmed_by} />
                  </div>
                  <span className="mt-0.5 block text-xs text-fg-3">{s.detail}</span>
                </div>
              ))}
            </Section>

            <Section title="评估 Evaluation">
              <div className="card space-y-1.5 px-3 py-2 text-sm">
                <p className="text-fg-2">
                  指标：<span className="font-mono text-[12px] text-fg">{plan.evaluation_config.metrics.join(", ")}</span>
                  {" · "}目标：<span className="font-mono text-[12px] text-brand">{plan.evaluation_config.target_metric}</span>
                </p>
                {plan.evaluation_config.reported_value != null && (
                  <p className="font-mono text-[10.5px] text-fg-3">
                    原文报告：{plan.evaluation_config.reported_value}
                    {plan.evaluation_config.reported_condition
                      ? `（${plan.evaluation_config.reported_condition}）`
                      : ""}
                    {plan.evaluation_config.reported_source_page
                      ? ` · p.${plan.evaluation_config.reported_source_page}`
                      : ""}
                  </p>
                )}
                <p className="text-xs text-warn/90">{plan.evaluation_config.comparison_note}</p>
              </div>
              {editables
                .filter((e) => e.path === "evaluation_config.tolerance")
                .map((item) => (
                  <EditRow
                    key={item.path}
                    item={item}
                    delegated={delegated}
                    active={selected?.path === item.path}
                    onClick={() => { setEditPrefill(null); setSelected(item); }}
                  />
                ))}
            </Section>

            <Section title="运行预算 Runtime">
              {editables
                .filter((e) => e.path === "runtime_budget.max_seconds")
                .map((item) => (
                  <EditRow
                    key={item.path}
                    item={item}
                    delegated={delegated}
                    active={selected?.path === item.path}
                    onClick={() => { setEditPrefill(null); setSelected(item); }}
                  />
                ))}
              <div className="card px-3 py-2 font-mono text-[10.5px] leading-relaxed text-fg-3">
                <p>
                  device {plan.runtime_budget.device} · est {plan.expected_runtime.estimated_seconds}s ·
                  mem ~{plan.resource_estimates.estimated_memory_mb ?? "—"}MB · disk ~
                  {plan.resource_estimates.estimated_disk_mb ?? "—"}MB
                </p>
                <p className="mt-1">{plan.expected_runtime.basis}</p>
                {plan.resource_estimates.note && <p className="mt-1">{plan.resource_estimates.note}</p>}
              </div>
            </Section>
          </section>

          <section className="rise rise-2 space-y-5">
            {selected ? (
              <div className="card space-y-3 p-4">
                <div className="flex items-center justify-between gap-2">
                  <h3 className="display min-w-0 truncate text-sm font-semibold">{selected.label}</h3>
                  {selected.param && (
                    <SourceBadge source={selected.param.source} confirmed={selected.param.human_confirmed} by={selected.param.confirmed_by} />
                  )}
                </div>
                {selected.param?.evidence_quote && (
                  <blockquote className="quote">
                    “{selected.param.evidence_quote}”
                    {selected.param.source_page != null && (
                      <span className="ml-2 font-mono text-[10.5px] not-italic text-fg-3">
                        p.{selected.param.source_page}
                      </span>
                    )}
                  </blockquote>
                )}
                {selected.param?.rationale && (
                  <p className="text-xs text-warn/90">{selected.param.rationale}</p>
                )}
                <input
                  key={`${selected.path}-${current?.version}-${editPrefill ?? ""}`}
                  ref={valueInput}
                  disabled={operationBusy}
                  defaultValue={editPrefill ?? (selected.value === null ? "" : String(selected.value))}
                  className="input"
                  aria-label="参数值"
                />
                <input
                  ref={reasonInput}
                  disabled={operationBusy}
                  aria-label="修改原因"
                  placeholder="修改原因（必填，记入版本历史）"
                  className="input"
                />
                <label className="flex items-center gap-2 text-sm text-fg-2">
                  <input key={`${selected.path}:${mode}`} ref={confirmCheck} type="checkbox" defaultChecked={mode === "expert"} disabled={operationBusy} className="h-3.5 w-3.5 accent-[var(--color-brand)]" />
                  标记为已人工确认
                </label>
                <button onClick={saveSelected} disabled={operationBusy} className="btn btn-primary w-full">
                  {saving ? "保存中…" : "保存为新版本"}
                </button>
              </div>
            ) : (
              <p className="rounded-xl border border-dashed border-line p-4 text-sm text-fg-3">
                点击左侧参数可编辑或确认；每次修改都会生成新版本，旧版本保留。
              </p>
            )}

            <div className="card space-y-3 p-4">
              <h2 className="label text-info!">AI 辅助审核 · 当前版本</h2>
              <ReviewRequestStatus label="通俗解读" loading={explanations.loading} error={explanations.error}
                loaded={!!explanations.data} disabled={operationBusy || !hasPending} onLoad={explanations.load} onExpert={() => setMode("expert")} />
              <ReviewRequestStatus label="AI 推荐" loading={advice.loading} error={advice.error}
                loaded={!!advice.data} disabled={operationBusy || !hasPending} onLoad={advice.load} onExpert={() => setMode("expert")} />
              {advice.data && <>
                <PreselectionHint threshold={advice.data.selection_threshold} />
                <p className="text-xs text-fg-2">下方关键及常规卡片展示全部 {recommendations.length} 项推荐；采纳会创建新版本，不会自动批准。</p>
                <div className="flex flex-wrap gap-2">
                  <button className="btn btn-info btn-sm" disabled={adviceDisabled || selectedPaths.length === 0}
                    onClick={() => adoptRecommendations(recommendations.filter((r) => selectedPaths.includes(r.field_path)))}>
                    采纳已选推荐（{selectedPaths.length}）
                  </button>
                  <button className="btn btn-primary btn-sm" disabled={adviceDisabled || recommendations.length === 0}
                    onClick={() => adoptRecommendations(recommendations)}>
                    采纳全部 AI 推荐（含 {criticalRecommendations} 项关键假设）
                  </button>
                </div>
              </>}
              {!hasPending && <p className="text-xs text-fg-3">本版本没有待审核假设。</p>}
            </div>

            <div className="space-y-4">
              <Section title={`关键假设 Critical（${groups.critical.length}）`}>
                {groups.critical.length === 0 && <p className="text-sm text-fg-3">（无）</p>}
                {groups.critical.map(renderAssumption)}
              </Section>

              <Section title={`常规假设 Routine（${groups.routine.length}）`}>
                {routinePending.length > 0 && <p className="text-xs text-fg-3">人工批量确认当前值，不采用 AI 修改建议。</p>}
                {routinePending.length > 0 && (
                  <button onClick={acceptRoutine} disabled={operationBusy} className="btn btn-info btn-sm w-full">
                    <IconCheck className="h-3 w-3" />
                    {saving ? "采纳中…" : `一键采纳全部常规项（${routinePending.length}）`}
                  </button>
                )}
                {groups.routine.length === 0 && <p className="text-sm text-fg-3">（无）</p>}
                {groups.routine.map(renderAssumption)}
              </Section>

              {groups.info.length > 0 && (
                <details>
                  <summary className="label cursor-pointer text-fg-2! focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand">
                    说明 Info（{groups.info.length}）· 仅告知，不需确认
                  </summary>
                  <div className="mt-1.5 space-y-1.5">
                    {groups.info.map(renderAssumption)}
                  </div>
                </details>
              )}
            </div>

            <Section title={`与原文偏差 Deviations（${plan.deviations_from_paper.length}）`}>
              {plan.deviations_from_paper.length === 0 && (
                <p className="text-sm text-fg-3">（无）</p>
              )}
              {plan.deviations_from_paper.map((d) => (
                <div key={d.field_path} className="card px-3 py-2 text-sm">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate font-mono text-[10.5px] text-fg-3">{d.field_path}</span>
                    <span className="shrink-0 font-mono text-[10.5px]">
                      <span className="text-fg-3">原文 {d.paper_value ?? "—"}</span>
                      <span className="mx-1 text-fg-3">→</span>
                      <span className="text-scale">{d.plan_value}</span>
                    </span>
                  </div>
                  <p className="mt-0.5 text-xs text-fg-3">{d.reason}</p>
                </div>
              ))}
            </Section>

            <Section title="版本历史 Versions">
              <div className="space-y-1.5">
                {versions.map((v) => (
                  <button
                    key={v.id}
                    onClick={() => switchVersion(v.version)}
                    disabled={operationBusy}
                    className={`field-row cursor-pointer! ${current?.version === v.version ? "field-row-active" : ""}`}
                  >
                    <span className="min-w-0">
                      <span className="block font-mono text-[12px] text-fg">
                        v{v.version} · {ORIGIN_LABEL[v.origin] ?? v.origin}
                      </span>
                      {v.change_reason && (
                        <span className="block truncate text-[11px] text-fg-3">{v.change_reason}</span>
                      )}
                    </span>
                    {v.approved_at && <span className="chip badge-ok shrink-0">已批准</span>}
                  </button>
                ))}
              </div>
              {current && current.changes.length > 0 && (
                <div className="card space-y-1 bg-ink-950/70 px-3 py-2 font-mono text-[10.5px] text-fg-2">
                  <p className="label text-[9px]!">本版本改动（前 → 后）</p>
                  {current.changes.map((c) => (
                    <p key={c.id} className="break-all">
                      {c.field_path}: {String(c.before)} → {String(c.after)}
                      {c.confirmed_by && <> · <ConfirmationBadge by={c.confirmed_by} /></>}
                      {c.review_mode && <span> · {c.review_mode === "delegated" ? "托管模式" : c.review_mode === "assisted" ? "辅助模式" : "专家模式"}</span>}
                      {c.actor && <span> · 操作者 {c.actor}</span>}
                      {c.timestamp && <span> · {c.timestamp}</span>}
                      {c.recommendation_id && <span> · 推荐 {c.recommendation_id}</span>}
                      {c.recommendation_confidence != null && <span> · 推荐置信度 {(c.recommendation_confidence * 100).toFixed(0)}%</span>}
                      {c.preselection_threshold != null && <span> · 预选阈值 {(c.preselection_threshold * 100).toFixed(0)}%</span>}
                    </p>
                  ))}
                  {current.related_error && (
                    <p className="mt-1 text-bad">关联错误：{current.related_error}</p>
                  )}
                </div>
              )}
            </Section>
          </section>
        </div>
      )}
      {!plan && !error && (
        <div className="space-y-3">
          <div className="skeleton h-24" />
          <div className="skeleton h-64" />
        </div>
      )}

      {plan && !approved && (
        <div className="sticky bottom-0 z-20 -mx-5 mt-2 border-t border-line bg-ink-950/85 px-5 py-3 backdrop-blur">
          <div className="flex flex-wrap items-center gap-3">
            <span
              className={`flex items-center gap-2 text-sm ${criticalPending.length ? "text-bad" : "text-ok"}`}
            >
              {criticalPending.length ? (
                <IconWarn className="h-4 w-4" />
              ) : (
                <IconCheck className="h-4 w-4" />
              )}
              {criticalPending.length
                ? `还需确认 ${criticalPending.length} 项关键假设`
                : "关键假设已齐，可批准"}
            </span>
            <span className="hidden font-mono text-[10.5px] text-fg-3 sm:inline">
              关键 {groups.critical.length} · 常规 {groups.routine.length} · 说明 {groups.info.length}
            </span>
            <span className="flex items-center gap-2 font-mono text-[10.5px] text-fg-3">
              已确认 {handledCount} / {decisionItems.length}
              <span
                role="progressbar"
                aria-label="假设确认进度"
                aria-valuenow={handledCount}
                aria-valuemin={0}
                aria-valuemax={decisionItems.length}
                className="h-1.5 w-24 overflow-hidden rounded-full bg-line"
              >
                <span
                  className={`block h-full transition-all ${progressPct === 100 ? "bg-ok" : "bg-brand"}`}
                  style={{ width: `${progressPct}%` }}
                />
              </span>
            </span>
            <span className="flex-1" />
            {routinePending.length > 0 && (
              <button onClick={acceptRoutine} disabled={operationBusy} className="btn btn-ghost btn-sm">
                一键采纳常规项（{routinePending.length}）
              </button>
            )}
            <button
              onClick={approve}
              disabled={!canApprove || approving}
              className="btn btn-primary btn-sm"
            >
              <IconCheck className="h-3.5 w-3.5" />
              {approving
                ? "提交中…"
                : reviewingDelegatedNormally
                  ? `按${mode === "expert" ? "专家" : "辅助"}模式重新校验并批准`
                  : workflowDriven && wfStatus === "awaiting_plan_approval"
                    ? "批准并执行"
                    : "批准计划"}
            </button>
          </div>
        </div>
      )}

      <RunPanel
        experimentId={experimentId}
        approved={approved}
        workflowDriven={workflowDriven}
        workflowStatus={wfStatus}
      />
    </main>
  );
}

export default function PlanClient({ experimentId }: { experimentId: string }) {
  return (
    <AuthGate>
      <Shell>
        <Suspense fallback={<div className="p-6 text-fg-3">加载中…</div>}>
          <PlanInner key={experimentId} experimentId={experimentId} />
        </Suspense>
      </Shell>
    </AuthGate>
  );
}