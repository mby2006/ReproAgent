"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import AuthGate from "@/components/AuthGate";
import Shell from "@/components/Shell";
import WorkflowInsights from "@/components/WorkflowInsights";
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
import { RISK_META, SOURCE_META, TONE_BADGE, fmtDate, tierOf } from "@/lib/ui";
import { fieldLabel, kindHint, shortValue } from "@/lib/fieldLabels";

const EXPERIMENT_STATUS: Record<string, string> = {
  planning: "计划中",
  approved: "已批准",
  succeeded: "已完成",
  failed: "已失败",
};

const REVIEW_MODE_TEXT: Record<string, string> = {
  expert: "人工逐项确认",
  assisted: "AI 给建议，你来决定",
  delegated: "交给系统自动处理",
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

function executableMetric(name: string) {
  return ["accuracy", "test accuracy", "top1 accuracy", "top-1 accuracy", "error", "error rate",
    "test error", "test error rate", "misclassification rate", "loss", "test loss",
    "cross entropy loss"].includes(name.trim().toLowerCase().replaceAll("_", " "));
}

function buildEditables(plan: ExperimentPlan): Editable[] {
  const t = plan.training_config;
  const list: Editable[] = [
    { path: "training_config.optimizer", label: "优化器", value: t.optimizer.value, param: t.optimizer },
    { path: "training_config.learning_rate", label: "学习率", value: t.learning_rate.value, param: t.learning_rate },
    { path: "training_config.loss_function", label: "损失函数", value: t.loss_function.value, param: t.loss_function },
    { path: "training_config.batch_size", label: "批大小", value: t.batch_size.value, param: t.batch_size },
    { path: "training_config.epochs", label: "训练轮数（epoch）", value: t.epochs.value, param: t.epochs },
  ];
  if (t.weight_decay) {
    list.push({ path: "training_config.weight_decay", label: "权重衰减", value: t.weight_decay.value, param: t.weight_decay });
  }
  list.push(
    { path: "dataset_config.train_subset_size", label: "训练子集大小", value: plan.dataset_config.train_subset_size },
    { path: "dataset_config.test_subset_size", label: "测试子集大小", value: plan.dataset_config.test_subset_size },
    { path: "runtime_budget.max_seconds", label: "时间预算 (秒)", value: plan.runtime_budget.max_seconds },
    { path: "evaluation_config.target_metric", label: "执行目标（accuracy / error_rate / test_loss）", value: plan.evaluation_config.target_metric },
    { path: "evaluation_config.tolerance", label: "对比容差", value: plan.evaluation_config.tolerance },
  );
  return list;
}

function EditRow({ item, active, onClick, delegated }: { item: Editable; active: boolean; onClick: () => void; delegated: boolean }) {
  const p = item.param;
  return (
    <button onClick={onClick} className={`field-row ${active ? "field-row-active" : ""}`}>
      <span className="min-w-0">
        <span className="block text-[13px] text-fg-3">{item.label}</span>
        <span className="block truncate font-mono text-[14.5px] text-fg">
          {item.value === null ? "—" : String(item.value)}
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-1.5">
        {p?.source === "mvp_scaling" && p.paper_value != null && (
          <span className="text-[12px] text-fg-3">原文 {String(p.paper_value)}</span>
        )}
        {p && p.confirmed_by !== "auto" && (
          <SourceBadge source={p.source} confirmed={p.human_confirmed} by={p.confirmed_by} />
        )}
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
  const kind = kindHint(a.kind);
  const pending = a.requires_confirmation && !isReviewHandled(a, delegated);
  return (
    <div className={`card space-y-1.5 border-l-2 p-3 text-sm ${TIER_BORDER[tier]}`}>
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="text-[15px] font-semibold text-fg">{fieldLabel(a.field_path)}</p>
          <p className="mt-0.5 text-[12.5px] text-fg-3">{kind.label}</p>
        </div>
        <select
          className={`select w-auto! shrink-0 py-0.5! pl-2! pr-6! text-[12px] ${TONE_BADGE[meta.tone]}`}
          value={tier}
          disabled={tierDisabled}
          aria-label={`调整「${fieldLabel(a.field_path)}」的重要程度`}
          title={`${meta.hint}。改动会生成新版本并留痕。`}
          onChange={(event) => onTierChange(event.target.value as RiskTier)}
        >
          <option value="critical">关键</option>
          <option value="routine">次要</option>
          <option value="info">仅告知</option>
        </select>
      </div>
      <p className="text-[13.5px] leading-relaxed text-fg-2">{kind.explain}</p>
      <div className="mt-1 rounded-lg border border-line-soft bg-ink-950/60 px-3 py-2">
        <span className="text-[12.5px] text-fg-3">这次实际用</span>
        <span className="ml-2 break-all font-mono text-[14px] text-fg">{shortValue(a.value, 90)}</span>
      </div>
      {a.rationale && <p className="text-[12.5px] leading-relaxed text-fg-3">这样取的原因：{a.rationale}</p>}
      {a.evidence_quote && (
        <details className="rounded-lg border border-line-soft bg-ink-950/40 px-3 py-2">
          <summary className="cursor-pointer text-[12.5px] text-fg-3">
            看论文里怎么写的{a.source_page != null ? `（第 ${a.source_page} 页）` : ""}
          </summary>
          <blockquote className="quote mt-2">
            “{a.evidence_quote}”
          </blockquote>
        </details>
      )}
      {explanation && <ExplanationCard explanation={explanation} mode={mode} />}
      {pending && recommendation && (
        <RecommendationCard recommendation={recommendation} currentValue={a.value} checked={checked}
          disabled={adviceDisabled} onCheck={onCheck} onAdopt={onAdopt} onModify={onModify} />
      )}
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

type ExperimentView = "plan" | "run" | "versions";

function PlanInner({ experimentId }: { experimentId: string }) {
  const search = useSearchParams();
  const router = useRouter();
  const view = (search.get("view") ?? "plan") as ExperimentView;
  const goView = useCallback(
    (next: ExperimentView) => {
      const params = new URLSearchParams(search.toString());
      if (next === "plan") params.delete("view");
      else params.set("view", next);
      router.replace(params.toString() ? `?${params.toString()}` : "?", { scroll: false });
      window.scrollTo({ top: 0, behavior: "smooth" });
    },
    [router, search],
  );
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
  const planHeadline = !current
    ? "正在加载实验计划…"
    : approved
      ? storedDelegated
        ? `计划已于 ${current.approved_at ? fmtDate(current.approved_at) : "此前"} 按「托管」方式批准：关键设置有系统自动接受，可以直接执行，但结果只能当趋势参考。`
        : `计划已于 ${current.approved_at ? fmtDate(current.approved_at) : "此前"} 批准，可以开始生成代码并训练。`
      : criticalPending.length > 0
        ? `还有 ${criticalPending.length} 项关键设置需要你决定，确认后即可开始训练。`
        : "计划已生成，确认参数无误后点击「批准并执行」。";
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
      <header className="workspace-heading space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <Link
            href={
              experiment
                ? `/papers?id=${experiment.paper_id}${workflowDriven ? `&workflow=${workflowId}` : ""}`
                : "/papers"
            }
            className="btn btn-ghost btn-sm"
            aria-label="返回论文"
          >
            <IconArrowLeft className="h-4 w-4" />
          </Link>
          <div className="workspace-title">
            <p className="label mb-1">实验计划</p>
            <h1 className="display text-2xl font-bold">
              {experiment?.name ?? "实验计划"}
            </h1>
          </div>
          {experiment && (
            <span className="chip">{EXPERIMENT_STATUS[experiment.status] ?? experiment.status}</span>
          )}
          {versions.length > 0 && (
            <select
              value={current?.version ?? ""}
              disabled={operationBusy}
              onChange={(e) => switchVersion(Number(e.target.value))}
              className="select w-52!"
              aria-label="计划版本"
            >
              {versions.map((v) => (
                <option key={v.id} value={v.version}>
                  第 {v.version} 版 · {ORIGIN_LABEL[v.origin] ?? v.origin}
                  {v.approved_at ? " · 已批准" : ""}
                </option>
              ))}
            </select>
          )}
        </div>

        <div className="flex flex-wrap items-center gap-3 rounded-lg bg-ink-800 px-4 py-3">
          <p className="min-w-52 flex-1 text-[15px] leading-relaxed text-fg-2">{planHeadline}</p>
          <button
            onClick={approve}
            disabled={!canApprove || approving}
            className="btn btn-primary"
          >
            <IconCheck className="h-4 w-4" />
            {approving
              ? "提交中…"
              : reviewingDelegatedNormally
                ? `按「${REVIEW_MODE_TEXT[mode] ?? mode}」重新校验并批准`
                : approved
                  ? storedDelegated ? "已托管批准 · 未人工核验" : "已批准"
                  : workflowDriven && wfStatus === "awaiting_plan_approval"
                    ? "批准并执行"
                    : "批准计划"}
          </button>
          {workflowDriven && wfStatus === "awaiting_plan_approval" && approved && (
            <button onClick={continueWorkflow} disabled={operationBusy || !isLatest} className="btn btn-info">
              {continuing ? "提交中…" : "继续执行"}
            </button>
          )}
          <button className="btn btn-ghost btn-sm" disabled={operationBusy} onClick={() => {
            setError(null);
            Promise.all([loadExperiment(), loadVersions()]).catch((err) => setError(err.message));
          }}>刷新</button>
        </div>
      </header>

      <nav className="experiment-nav grid gap-2 sm:grid-cols-3" aria-label="实验步骤">
        {([
          {
            key: "plan",
            no: "1",
            title: "设置与确认",
            hint: criticalPending.length
              ? `还有 ${criticalPending.length} 项要你决定`
              : "设置已确认，可以开始",
          },
          { key: "run", no: "2", title: "训练与结果", hint: "跑完看结论和报告" },
          { key: "versions", no: "3", title: "修改记录", hint: `共 ${versions.length} 个版本` },
        ] as const).map((item) => {
          const active = view === item.key;
          return (
            <button
              key={item.key}
              onClick={() => goView(item.key)}
              aria-current={active ? "step" : undefined}
              className={`rounded-xl border px-4 py-3 text-left transition ${
                active
                  ? "border-brand/50 bg-brand/8"
                  : "border-line bg-ink-900/60 hover:border-fg-3/30"
              }`}
            >
              <span className="flex items-center gap-2">
                <span
                  className={`flex h-5 w-5 items-center justify-center rounded-full text-[12px] ${
                    active ? "bg-brand text-ink-950" : "bg-line text-fg-3"
                  }`}
                >
                  {item.no}
                </span>
                <span className={`text-[15px] font-semibold ${active ? "text-fg" : "text-fg-2"}`}>
                  {item.title}
                </span>
              </span>
              <span className="mt-1 block text-[12.5px] text-fg-3">{item.hint}</span>
            </button>
          );
        })}
      </nav>

      {workflow && <WorkflowInsights key={workflow.id} workflow={workflow} onRetry={retry} retrying={retrying} />}

      {!approved && criticalPending.length > 0 && (
        <p className="rise flex flex-wrap items-center gap-2 rounded-lg border border-bad/40 bg-bad/10 px-4 py-3 text-[14px] text-bad">
          <IconWarn className="h-4 w-4 shrink-0" />
          还需确认 {criticalPending.length} 项关键设置才能开始训练，详见下方「关键设置」：
          <span className="text-[13px]">这些是会影响实验本质的取值，下方「关键设置」里已用红色标出</span>
        </p>
      )}
      {shownError && (
        <div role="alert" className="rise space-y-2 rounded-lg border border-bad/40 bg-bad/10 px-4 py-3 text-[14px] text-bad">
          <p>{shownError}</p>
          {storedDelegated && <button className="btn btn-ghost btn-sm" disabled={operationBusy} onClick={() => setMode("expert")}>切换人工逐项确认</button>}
        </div>
      )}

      {view === "plan" && (
      <details className="card px-4 py-3">
        <summary className="cursor-pointer list-none text-[13.5px] text-fg-2">
          审核方式：<span className="text-fg">{REVIEW_MODE_TEXT[mode] ?? mode}</span>
          <span className="ml-2 text-fg-3">（点开可切换，一般用不到）</span>
        </summary>
        <div className="mt-3 space-y-3">
          <ReviewModeSelector mode={mode} onChange={setMode} disabled={operationBusy || !current}
            delegationDisabled={writeDisabled} onDelegate={() => { if (!writeDisabled) setDelegationTarget(current); }} />
          {delegationTarget && <DelegationDialog version={delegationTarget.version} workflowDriven={workflowDriven}
            budget={`${delegationTarget.plan.runtime_budget.max_seconds} 秒 · ${delegationTarget.plan.runtime_budget.device}`}
            onConfirm={authorizeDelegation} onCancel={() => setDelegationTarget(null)}
            onExpert={() => { setDelegationTarget(null); setMode("expert"); }} />}
          {plan?.review_mode === "delegated" && <DelegationNotice delegatedAt={plan.delegated_at} delegatedBy={plan.delegated_by} />}
          {reviewingDelegatedNormally && <p role="status" className="rounded-lg border border-warn/40 bg-warn/10 px-4 py-3 text-[13.5px] text-warn">
            只切换了本页的显示方式，还没保存。原版本{savedApproved ? `已于 ${current?.approved_at} 按托管方式批准` : "的托管记录仍然有效"}，
            不代表已按新模式审核。按新模式重新批准会重新校验关键设置并撤销原授权。
          </p>}
          {current && !isLatest && <p className="text-[13.5px] text-warn">你正在查看历史版本。要批准或让 AI 采纳建议，请先点上方「刷新」回到最新版。</p>}
          {loading && <p role="status" className="text-[13px] text-fg-3">正在加载计划…</p>}
        </div>
      </details>
      )}

      {plan && view === "plan" && (
        <>
        <div className="grid gap-5 lg:grid-cols-2">
          <section className="rise rise-1 space-y-5">
            <div className="card border-scale/30 bg-scale/8 p-4">
              <div className="mb-2 flex flex-wrap items-center gap-2">
                <span className="chip badge-scale">缩小规模运行</span>
                <span className="text-[12px] text-fg-3" title={plan.scaling_profile}>
                  策略：{plan.scaling_profile}
                </span>
              </div>
              <p className="text-[13.5px] leading-relaxed text-fg-2">{plan.rationale}</p>
            </div>

            <Section title="训练设置（点一项即可修改或确认）">
              {delegated && (
                <p className="mb-1 text-[12.5px] leading-relaxed text-warn/90">
                  以下取值由系统自动接受，没有人逐项看过；要改可以直接点开改。
                </p>
              )}
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
              <p className="font-mono text-[12px] text-fg-3">
                seed = {plan.training_config.seed}（固定）
              </p>
            </Section>

            <Section title="数据集">
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
              <p className="font-mono text-[12px] text-fg-3">
                {plan.dataset_config.name} · input {plan.dataset_config.input_shape.join("×")} ·{" "}
                {plan.dataset_config.num_classes} classes · full train{" "}
                {plan.dataset_config.full_train_size ?? "—"} / test{" "}
                {plan.dataset_config.full_test_size ?? "—"}
              </p>
            </Section>

            <Section title="模型结构">
              <div className="card px-3 py-2 text-sm">
                <span className="font-mono text-[13px] text-fg">{plan.model_config.architecture}</span>
                {plan.model_config.description && (
                  <span className="mt-0.5 block text-xs text-fg-3">{plan.model_config.description}</span>
                )}
              </div>
            </Section>

            <Section title="数据预处理">
              {plan.preprocessing_config.steps.map((step) => (
                <div key={step.name} className="card px-3 py-2.5 text-sm">
                  <span className="text-[14px] text-fg">{fieldLabel(`preprocessing_config.${step.name}`)}</span>
                  {step.detail && (
                    <details className="mt-1">
                      <summary className="cursor-pointer text-[12.5px] text-fg-3">
                        看具体做法{step.detail.length > 40 ? "（论文原文）" : ""}
                      </summary>
                      <p className="mt-1 text-[12.5px] leading-relaxed text-fg-3">{step.detail}</p>
                    </details>
                  )}
                </div>
              ))}
            </Section>

            <Section title="评估方式">
              <div className="card space-y-1.5 px-3 py-2 text-sm">
                <p className="text-fg-2">
                  指标：<span className="font-mono text-[12px] text-fg">{plan.evaluation_config.metrics.join(", ")}</span>
                  {" · "}目标：<span className="font-mono text-[12px] text-brand">{plan.evaluation_config.target_metric}</span>
                </p>
                {plan.evaluation_config.reported_value != null && (
                  <p className="font-mono text-[12px] text-fg-3">
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
              {!executableMetric(plan.evaluation_config.target_metric) && <p role="alert" className="rounded-lg bg-warn/10 p-3 text-sm text-warn">
                当前指标无法在本执行器中计算。请修改下方「执行目标」，选择 accuracy（准确率）、error_rate（错误率）或 test_loss（测试损失），保存后再批准。
              </p>}
              {editables
                .filter((e) => e.path.startsWith("evaluation_config."))
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

            <Section title="运行预算">
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
              <div className="card px-3 py-2 font-mono text-[12px] leading-relaxed text-fg-3">
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
                      <span className="ml-2 font-mono text-[12px] not-italic text-fg-3">
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
                点击左侧任意一项即可修改取值或确认。每次修改都会保存为新版本，旧版本不会被覆盖。
              </p>
            )}

            <div className="card space-y-3 p-4">
              <h2 className="label text-info!">AI 可以帮你审一遍（可选）</h2>
              <p className="text-[13px] leading-relaxed text-fg-3">
                让 AI 解释每项设置在做什么，或直接给出建议值。采纳建议会生成新版本，不会替你批准。
              </p>
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
              {!hasPending && <p className="text-xs text-fg-3">当前版本没有需要你确认的项。</p>}
            </div>

            <div className="space-y-4">
              <Section title={`必须你决定的设置（${groups.critical.length} 项）`}>
                <p className="mb-2 text-[13px] leading-relaxed text-fg-3">
                  这些取值会改变实验本质（例如换掉论文原本的优化器或损失函数）。确认前不会开始训练。
                </p>
                {groups.critical.length === 0 && <p className="text-sm text-fg-3">（无）</p>}
                {groups.critical.map(renderAssumption)}
              </Section>

              <Section title={`影响较小的设置（${groups.routine.length} 项）`}>
                <p className="mb-2 text-[13px] leading-relaxed text-fg-3">
                  论文没写细节、但有安全默认值的项，可以一次性全部接受。
                </p>
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
                    仅作说明（{groups.info.length} 项）· 不需要操作
                  </summary>
                  <div className="mt-1.5 space-y-1.5">
                    {groups.info.map(renderAssumption)}
                  </div>
                </details>
              )}
            </div>

          </section>
        </div>
        <Section title={`和论文不一样的地方（${plan.deviations_from_paper.length} 项）`}>
          <p className="mb-2 text-[13px] leading-relaxed text-fg-3">
            为了在几分钟内跑完而有意缩小的规模。这些改动会拉低数值，但不改变方法本身的正确性。
          </p>
          {plan.deviations_from_paper.length === 0 && (
            <p className="text-sm text-fg-3">（无）</p>
          )}
          {plan.deviations_from_paper.map((d) => (
            <div key={d.field_path} className="card px-3 py-2 text-sm">
              <div className="flex items-center justify-between gap-2">
                <span className="truncate font-mono text-[12px] text-fg-3">{d.field_path}</span>
                <span className="shrink-0 font-mono text-[12px]">
                  <span className="text-fg-3">原文 {d.paper_value ?? "—"}</span>
                  <span className="mx-1 text-fg-3">→</span>
                  <span className="text-scale">{d.plan_value}</span>
                </span>
              </div>
              <p className="mt-0.5 text-xs text-fg-3">{d.reason}</p>
            </div>
          ))}
        </Section>
        </>
      )}
      {!plan && !error && (
        <div className="space-y-3">
          <div className="skeleton h-24" />
          <div className="skeleton h-64" />
        </div>
      )}

      {plan && !approved && view === "plan" && (
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
            <span className="hidden font-mono text-[12px] text-fg-3 sm:inline">
              关键 {groups.critical.length} · 常规 {groups.routine.length} · 说明 {groups.info.length}
            </span>
            <span className="flex items-center gap-2 font-mono text-[12px] text-fg-3">
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

      {view === "versions" && (
        <section className="rise space-y-3">
          <div className="card p-4">
            <p className="text-[15px] font-semibold text-fg">
              这份计划一共改过 {versions.length} 次
            </p>
            <p className="mt-1 text-[13.5px] leading-relaxed text-fg-2">
              每次修改都会存成新版本，旧版本不会被覆盖，随时可以切回去看。
              下面先列每一版的摘要，再说明这一版具体改了哪几项。
            </p>
          </div>

          <div className="space-y-2">
            {versions.map((v) => (
              <button
                key={v.id}
                onClick={() => switchVersion(v.version)}
                disabled={operationBusy}
                className={`card w-full px-4 py-3 text-left transition ${
                  current?.version === v.version ? "border-brand/50" : "hover:border-fg-3/30"
                }`}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-[15px] font-semibold text-fg">第 {v.version} 版</span>
                  <span className="chip">{ORIGIN_LABEL[v.origin] ?? v.origin}</span>
                  {v.approved_at && <span className="chip badge-ok">已批准</span>}
                  <span className="ml-auto text-[12.5px] text-fg-3">{fmtDate(v.created_at)}</span>
                </div>
                <p className="mt-1 text-[13.5px] text-fg-2">
                  {v.change_reason || "这一版没有填写修改说明"}
                </p>
              </button>
            ))}
          </div>

          {current && current.changes.length > 0 && (
            <div className="card p-4">
              <p className="text-[15px] font-semibold text-fg">
                第 {current.version} 版具体改了 {current.changes.length} 处
              </p>
              <p className="mt-1 text-[13px] text-fg-3">
                只列「哪一项、从什么改成什么」。操作人、时间、推荐编号这些审计信息，收在每条的「技术信息」里。
              </p>
              <ul className="mt-3 space-y-2">
                {current.changes.map((c) => (
                  <li key={c.id} className="rounded-lg border border-line-soft bg-ink-950/50 px-3.5 py-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-[14.5px] font-medium text-fg">
                        {fieldLabel(c.field_path)}
                      </span>
                      {c.confirmed_by && <ConfirmationBadge by={c.confirmed_by} />}
                    </div>
                    <p className="mt-1.5 text-[13.5px] leading-relaxed text-fg-2">
                      <span className="text-fg-3">原来：</span>
                      {shortValue(c.before, 40)}
                      <span className="mx-1.5 text-fg-3">→</span>
                      <span className="text-fg">现在：{shortValue(c.after, 40)}</span>
                    </p>
                    <details className="mt-1.5">
                      <summary className="cursor-pointer text-[12px] text-fg-3">技术信息</summary>
                      <dl className="mt-1.5 grid gap-1 text-[12px] text-fg-3 sm:grid-cols-2">
                        <div>
                          <dt className="inline">字段：</dt>
                          <dd className="inline font-mono">{c.field_path}</dd>
                        </div>
                        {c.actor && (
                          <div>
                            <dt className="inline">操作人：</dt>
                            <dd className="inline break-all font-mono">{c.actor}</dd>
                          </div>
                        )}
                        {c.timestamp && (
                          <div>
                            <dt className="inline">时间：</dt>
                            <dd className="inline">{c.timestamp}</dd>
                          </div>
                        )}
                        {c.recommendation_id && (
                          <div>
                            <dt className="inline">推荐编号：</dt>
                            <dd className="inline break-all font-mono">{c.recommendation_id}</dd>
                          </div>
                        )}
                        {c.recommendation_confidence != null && (
                          <div>
                            <dt className="inline">AI 置信度：</dt>
                            <dd className="inline">
                              {(c.recommendation_confidence * 100).toFixed(0)}%
                            </dd>
                          </div>
                        )}
                      </dl>
                    </details>
                  </li>
                ))}
              </ul>
              {current.related_error && (
                <p className="mt-2 text-[13px] text-bad">关联错误：{current.related_error}</p>
              )}
            </div>
          )}
        </section>
      )}

      {view === "run" && (
        <RunPanel
          experimentId={experimentId}
          approved={approved}
          workflowDriven={workflowDriven}
          workflowStatus={wfStatus}
        />
      )}
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
