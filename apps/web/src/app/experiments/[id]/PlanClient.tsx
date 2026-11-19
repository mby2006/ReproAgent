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
import { apiFetch, resumeWorkflow, retryWorkflow } from "@/lib/api";
import { useWorkflow } from "@/lib/useWorkflow";
import type {
  Assumption,
  ExperimentDetail,
  ExperimentPlan,
  PlanParam,
  PlanVersion,
  PlanVersionMeta,
  RiskTier,
} from "@/lib/types";
import { RISK_META, SOURCE_META, TONE_BADGE, TONE_TEXT, tierOf } from "@/lib/ui";

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

function SourceBadge({ source, confirmed }: { source: string; confirmed?: boolean }) {
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

function EditRow({ item, active, onClick }: { item: Editable; active: boolean; onClick: () => void }) {
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
        {p && <SourceBadge source={p.source} confirmed={p.human_confirmed} />}
        {p?.requires_confirmation && !p.human_confirmed && (
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
}: {
  a: Assumption;
  saving: boolean;
  onConfirm: (a: Assumption) => void;
}) {
  const tier = tierOf(a);
  const meta = RISK_META[tier];
  const pending = a.requires_confirmation && !a.human_confirmed;
  return (
    <div className={`card space-y-1.5 border-l-2 p-3 text-sm ${TIER_BORDER[tier]}`}>
      <div className="flex items-center justify-between gap-2">
        <span className="truncate font-mono text-[10.5px] text-fg-3">{a.field_path}</span>
        <span className="flex shrink-0 items-center gap-1.5">
          <span className={`chip ${TONE_BADGE[meta.tone]}`}>{meta.label}</span>
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
      {pending ? (
        <button onClick={() => onConfirm(a)} disabled={saving} className="btn btn-info btn-sm">
          <IconCheck className="h-3 w-3" /> 确认此假设
        </button>
      ) : (
        <span className={`text-xs ${a.human_confirmed ? "text-ok" : "text-fg-3"}`}>
          {a.human_confirmed ? "✓ 已确认" : "无需确认（MVP 缩放/说明）"}
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
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [approving, setApproving] = useState(false);
  const [continuing, setContinuing] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const valueInput = useRef<HTMLInputElement>(null);
  const reasonInput = useRef<HTMLInputElement>(null);
  const confirmCheck = useRef<HTMLInputElement>(null);
  const { workflowId, workflow, refresh } = useWorkflow(search.get("workflow"));
  const wfStatus = workflow?.status;
  const workflowDriven = workflowId != null;

  const loadVersions = useCallback(async () => {
    const list = await apiFetch<PlanVersionMeta[]>(`/v1/experiments/${experimentId}/plans`);
    setVersions(list);
  }, [experimentId]);

  const loadExperiment = useCallback(async () => {
    const exp = await apiFetch<ExperimentDetail>(`/v1/experiments/${experimentId}`);
    setExperiment(exp);
    setCurrent(exp.plan);
    setSelected(null);
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
    setError(null);
    try {
      const pv = await apiFetch<PlanVersion>(`/v1/experiments/${experimentId}/plans/${v}`);
      setCurrent(pv);
      setSelected(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "加载版本失败");
    }
  }

  async function persistEdit(
    path: string,
    value: string | number | boolean | null,
    humanConfirmed: boolean,
    reason: string,
  ) {
    if (!current) return;
    setSaving(true);
    setError(null);
    try {
      await apiFetch<PlanVersion>(`/v1/experiments/${experimentId}/plans`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_version: current.version,
          changes: [{ field_path: path, value, human_confirmed: humanConfirmed }],
          change_reason: reason,
          origin: "user_edit",
        }),
      });
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
    persistEdit(selected.path, value, confirmCheck.current?.checked ?? true, reason);
  }

  function confirmAssumption(a: Assumption) {
    persistEdit(
      a.field_path,
      a.value as string | number | boolean | null,
      true,
      `确认假设：${a.field_path}`,
    );
  }

  async function acceptRoutine() {
    const p = current?.plan;
    if (!current || !p) return;
    const targets = p.assumptions.filter(
      (a) =>
        tierOf(a) === "routine" &&
        a.requires_confirmation &&
        !a.human_confirmed &&
        a.auto_acceptable,
    );
    if (targets.length === 0) return;
    setSaving(true);
    setError(null);
    try {
      await apiFetch<PlanVersion>(`/v1/experiments/${experimentId}/plans`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_version: current.version,
          changes: targets.map((a) => ({
            field_path: a.field_path,
            value: a.value,
            human_confirmed: true,
          })),
          change_reason: `批量采纳 ${targets.length} 项常规假设`,
          origin: "user_edit",
        }),
      });
      await loadVersions();
      await loadExperiment();
    } catch (err) {
      setError(err instanceof Error ? err.message : "批量采纳失败");
    } finally {
      setSaving(false);
    }
  }

  async function approve() {
    if (!current) return;
    setApproving(true);
    setError(null);
    try {
      await apiFetch<PlanVersion>(
        `/v1/experiments/${experimentId}/plans/${current.version}/approve`,
        { method: "POST" },
      );
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

  const plan = current?.plan;
  const editables = plan ? buildEditables(plan) : [];
  const assumptions = plan?.assumptions ?? [];
  const isPending = (a: Assumption) => a.requires_confirmation && !a.human_confirmed;
  const criticalPending = assumptions.filter((a) => isPending(a) && tierOf(a) === "critical");
  const routinePending = assumptions.filter(
    (a) => isPending(a) && tierOf(a) === "routine" && a.auto_acceptable,
  );
  const groups: Record<RiskTier, Assumption[]> = {
    critical: assumptions.filter((a) => tierOf(a) === "critical"),
    routine: assumptions.filter((a) => tierOf(a) === "routine"),
    info: assumptions.filter((a) => tierOf(a) === "info"),
  };
  const approved = current?.approved_at != null;
  const canApprove = criticalPending.length === 0 && !approved && !!current;

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
            : approved
              ? "已批准"
              : workflowDriven && wfStatus === "awaiting_plan_approval"
                ? "批准并执行"
                : "批准计划"}
        </button>
        {workflowDriven && wfStatus === "awaiting_plan_approval" && approved && (
          <button onClick={continueWorkflow} disabled={continuing} className="btn btn-info btn-sm">
            {continuing ? "提交中…" : "继续执行"}
          </button>
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

      {approved && (
        <p className="rise rounded-lg border border-ok/40 bg-ok/10 px-4 py-2 text-sm text-ok">
          该计划版本已批准（{current?.approved_at}），可进入代码生成阶段。
        </p>
      )}
      {!approved && criticalPending.length > 0 && (
        <p className="rise flex flex-wrap items-center gap-2 rounded-lg border border-bad/40 bg-bad/10 px-4 py-2 text-sm text-bad">
          <IconWarn className="h-4 w-4 shrink-0" />
          还需确认 {criticalPending.length} 项关键假设后方可批准：
          <span className="font-mono text-xs">{criticalPending.map((a) => a.field_path).join("、")}</span>
        </p>
      )}
      {error && (
        <p className="rise rounded-lg border border-bad/40 bg-bad/10 px-4 py-2 text-sm text-bad">
          {error}
        </p>
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
                    active={selected?.path === item.path}
                    onClick={() => setSelected(item)}
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
                    active={selected?.path === item.path}
                    onClick={() => setSelected(item)}
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
                    <SourceBadge source={s.source} />
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
                    active={selected?.path === item.path}
                    onClick={() => setSelected(item)}
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
                    active={selected?.path === item.path}
                    onClick={() => setSelected(item)}
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
                    <SourceBadge source={selected.param.source} confirmed={selected.param.human_confirmed} />
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
                  key={`${selected.path}-${current?.version}`}
                  ref={valueInput}
                  defaultValue={selected.value === null ? "" : String(selected.value)}
                  className="input"
                  aria-label="参数值"
                />
                <input
                  ref={reasonInput}
                  placeholder="修改原因（必填，记入版本历史）"
                  className="input"
                />
                <label className="flex items-center gap-2 text-sm text-fg-2">
                  <input ref={confirmCheck} type="checkbox" defaultChecked className="h-3.5 w-3.5 accent-[var(--color-brand)]" />
                  标记为已人工确认
                </label>
                <button onClick={saveSelected} disabled={saving} className="btn btn-primary w-full">
                  {saving ? "保存中…" : "保存为新版本"}
                </button>
              </div>
            ) : (
              <p className="rounded-xl border border-dashed border-line p-4 text-sm text-fg-3">
                点击左侧参数可编辑或确认；每次修改都会生成新版本，旧版本保留。
              </p>
            )}

            <div className="space-y-4">
              <Section title={`关键假设 Critical（${groups.critical.length}）`}>
                {groups.critical.length === 0 && <p className="text-sm text-fg-3">（无）</p>}
                {groups.critical.map((a) => (
                  <AssumptionCard key={a.field_path} a={a} saving={saving} onConfirm={confirmAssumption} />
                ))}
              </Section>

              <Section title={`常规假设 Routine（${groups.routine.length}）`}>
                {routinePending.length > 0 && (
                  <button onClick={acceptRoutine} disabled={saving} className="btn btn-info btn-sm w-full">
                    <IconCheck className="h-3 w-3" />
                    {saving ? "采纳中…" : `一键采纳全部常规项（${routinePending.length}）`}
                  </button>
                )}
                {groups.routine.length === 0 && <p className="text-sm text-fg-3">（无）</p>}
                {groups.routine.map((a) => (
                  <AssumptionCard key={a.field_path} a={a} saving={saving} onConfirm={confirmAssumption} />
                ))}
              </Section>

              {groups.info.length > 0 && (
                <Section title={`说明 Info（${groups.info.length}）`}>
                  {groups.info.map((a) => (
                    <AssumptionCard key={a.field_path} a={a} saving={saving} onConfirm={confirmAssumption} />
                  ))}
                </Section>
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
                    <p key={c.field_path} className="break-all">
                      {c.field_path}: {String(c.before)} → {String(c.after)}
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
            <span className="flex-1" />
            {routinePending.length > 0 && (
              <button onClick={acceptRoutine} disabled={saving} className="btn btn-ghost btn-sm">
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
          <PlanInner experimentId={experimentId} />
        </Suspense>
      </Shell>
    </AuthGate>
  );
}