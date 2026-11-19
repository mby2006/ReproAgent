"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import AuthGate from "@/components/AuthGate";
import WorkflowInsights from "@/components/WorkflowInsights";
import WorkflowStepper from "@/components/WorkflowStepper";
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
} from "@/lib/types";

const SOURCE_LABEL: Record<string, string> = {
  paper_explicit: "原文",
  inferred: "推断",
  human_confirmed: "已确认",
  mvp_scaling: "MVP缩放",
};

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
  const color =
    source === "paper_explicit"
      ? "bg-emerald-800 text-emerald-200"
      : source === "mvp_scaling"
        ? "bg-violet-700 text-violet-100"
        : source === "human_confirmed"
          ? "bg-sky-700 text-sky-100"
          : "bg-amber-600 text-amber-50";
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs ${color}`}>
      {SOURCE_LABEL[source] ?? source}
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
    <button
      onClick={onClick}
      className={`flex w-full items-center justify-between gap-2 rounded-lg border px-3 py-2 text-left text-sm ${
        active ? "border-emerald-500 bg-zinc-800" : "border-zinc-800 bg-zinc-900 hover:border-zinc-600"
      }`}
    >
      <span className="min-w-0">
        <span className="block text-xs text-zinc-400">{item.label}</span>
        <span className="block truncate">{item.value === null ? "—" : String(item.value)}</span>
      </span>
      <span className="flex shrink-0 items-center gap-1.5">
        {p?.source === "mvp_scaling" && p.paper_value != null && (
          <span className="text-xs text-zinc-500">原文 {String(p.paper_value)}</span>
        )}
        {p && <SourceBadge source={p.source} confirmed={p.human_confirmed} />}
        {p?.requires_confirmation && !p.human_confirmed && <span className="text-xs text-amber-400">⚠</span>}
      </span>
    </button>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">{title}</h2>
      {children}
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
  const unconfirmed = plan
    ? plan.assumptions.filter((a) => a.requires_confirmation && !a.human_confirmed)
    : [];
  const approved = current?.approved_at != null;
  const canApprove = unconfirmed.length === 0 && !approved && !!current;

  return (
    <main className="mx-auto w-full max-w-7xl flex-1 space-y-4 p-6">
      <header className="flex flex-wrap items-center gap-3">
        <Link
          href={
            experiment
              ? `/papers/${experiment.paper_id}${workflowDriven ? `?workflow=${workflowId}` : ""}`
              : "/papers"
          }
          className="text-sm text-zinc-400 hover:text-zinc-200"
        >
          ← 返回论文
        </Link>
        <h1 className="min-w-0 flex-1 truncate text-xl font-semibold">
          {experiment?.name ?? "实验计划"}
        </h1>
        {experiment && (
          <span className="rounded bg-zinc-800 px-2 py-0.5 text-xs text-zinc-300">
            {experiment.status}
          </span>
        )}
        {versions.length > 0 && (
          <select
            value={current?.version ?? ""}
            onChange={(e) => switchVersion(Number(e.target.value))}
            className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-sm"
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
          className="rounded bg-emerald-600 px-3 py-1 text-sm hover:bg-emerald-500 disabled:opacity-50"
        >
          {approving
            ? "提交中…"
            : approved
              ? "已批准"
              : workflowDriven && wfStatus === "awaiting_plan_approval"
                ? "批准并执行"
                : "批准计划"}
        </button>
        {workflowDriven && wfStatus === "awaiting_plan_approval" && approved && (
          <button
            onClick={continueWorkflow}
            disabled={continuing}
            className="rounded bg-sky-600 px-3 py-1 text-sm hover:bg-sky-500 disabled:opacity-50"
          >
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
        <p className="rounded-lg border border-emerald-700 bg-emerald-950 px-4 py-2 text-sm text-emerald-300">
          该计划版本已批准（{current?.approved_at}），可进入代码生成阶段。
        </p>
      )}
      {!approved && unconfirmed.length > 0 && (
        <p className="rounded-lg border border-amber-700 bg-amber-950 px-4 py-2 text-sm text-amber-300">
          存在 {unconfirmed.length} 项未确认的关键假设，确认后方可批准：
          {unconfirmed.map((a) => a.field_path).join("、")}
        </p>
      )}
      {error && (
        <p className="rounded-lg border border-red-800 bg-red-950 px-4 py-2 text-sm text-red-300">
          {error}
        </p>
      )}

      {plan && (
        <div className="grid gap-4 lg:grid-cols-2">
          <section className="space-y-4">
            <div className="rounded-xl border border-violet-800 bg-violet-950/40 p-3 text-sm text-violet-200">
              <span className="mr-2 rounded bg-violet-700 px-1.5 py-0.5 text-xs">
                {plan.scaling_profile}
              </span>
              {plan.rationale}
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
              <p className="text-xs text-zinc-500">随机种子 seed = {plan.training_config.seed}（固定）</p>
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
              <p className="text-xs text-zinc-500">
                {plan.dataset_config.name} · 输入 {plan.dataset_config.input_shape.join("×")} ·{" "}
                {plan.dataset_config.num_classes} 类 · 全量 train{" "}
                {plan.dataset_config.full_train_size ?? "—"} / test{" "}
                {plan.dataset_config.full_test_size ?? "—"}
              </p>
            </Section>

            <Section title="模型 Model">
              <div className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm">
                <span className="text-zinc-400">{plan.model_config.architecture}</span>
                {plan.model_config.description && (
                  <span className="block text-xs text-zinc-500">{plan.model_config.description}</span>
                )}
              </div>
            </Section>

            <Section title="预处理 Preprocessing">
              {plan.preprocessing_config.steps.map((s) => (
                <div key={s.name} className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm">
                  <div className="flex items-center justify-between">
                    <span>{s.name}</span>
                    <SourceBadge source={s.source} />
                  </div>
                  <span className="block text-xs text-zinc-500">{s.detail}</span>
                </div>
              ))}
            </Section>

            <Section title="评估 Evaluation">
              <div className="space-y-1.5 rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm">
                <p>
                  指标：{plan.evaluation_config.metrics.join(", ")} · 目标：
                  {plan.evaluation_config.target_metric}
                </p>
                {plan.evaluation_config.reported_value != null && (
                  <p className="text-xs text-zinc-400">
                    原文报告：{plan.evaluation_config.reported_value}
                    {plan.evaluation_config.reported_condition
                      ? `（${plan.evaluation_config.reported_condition}）`
                      : ""}
                    {plan.evaluation_config.reported_source_page
                      ? ` · p.${plan.evaluation_config.reported_source_page}`
                      : ""}
                  </p>
                )}
                <p className="text-xs text-amber-300/80">{plan.evaluation_config.comparison_note}</p>
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
              <div className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-xs text-zinc-400">
                <p>
                  设备 {plan.runtime_budget.device} · 预计 {plan.expected_runtime.estimated_seconds}s ·
                  内存 ~{plan.resource_estimates.estimated_memory_mb ?? "—"}MB · 磁盘 ~
                  {plan.resource_estimates.estimated_disk_mb ?? "—"}MB
                </p>
                <p className="mt-1 text-zinc-500">{plan.expected_runtime.basis}</p>
                {plan.resource_estimates.note && (
                  <p className="mt-1 text-zinc-500">{plan.resource_estimates.note}</p>
                )}
              </div>
            </Section>
          </section>

          <section className="space-y-3">
            {selected ? (
              <div className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-900 p-4">
                <div className="flex items-center justify-between">
                  <h3 className="font-medium">{selected.label}</h3>
                  {selected.param && (
                    <SourceBadge source={selected.param.source} confirmed={selected.param.human_confirmed} />
                  )}
                </div>
                {selected.param?.evidence_quote && (
                  <blockquote className="rounded border-l-2 border-emerald-600 bg-zinc-950 px-3 py-2 text-sm italic text-zinc-300">
                    “{selected.param.evidence_quote}”
                    {selected.param.source_page != null && (
                      <span className="ml-2 not-italic text-xs text-zinc-500">
                        p.{selected.param.source_page}
                      </span>
                    )}
                  </blockquote>
                )}
                {selected.param?.rationale && (
                  <p className="text-xs text-amber-300/80">{selected.param.rationale}</p>
                )}
                <input
                  key={`${selected.path}-${current?.version}`}
                  ref={valueInput}
                  defaultValue={selected.value === null ? "" : String(selected.value)}
                  className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-1.5 text-sm"
                />
                <input
                  ref={reasonInput}
                  placeholder="修改原因（必填，记入版本历史）"
                  className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-1.5 text-sm"
                />
                <label className="flex items-center gap-2 text-sm text-zinc-300">
                  <input ref={confirmCheck} type="checkbox" defaultChecked className="accent-emerald-600" />
                  标记为已人工确认
                </label>
                <button
                  onClick={saveSelected}
                  disabled={saving}
                  className="w-full rounded bg-emerald-600 px-3 py-1.5 text-sm hover:bg-emerald-500 disabled:opacity-50"
                >
                  {saving ? "保存中…" : "保存为新版本"}
                </button>
              </div>
            ) : (
              <p className="rounded-xl border border-dashed border-zinc-800 p-4 text-sm text-zinc-500">
                点击左侧参数可编辑或确认；每次修改都会生成新版本，旧版本保留。
              </p>
            )}

            <Section title={`假设 Assumptions（${plan.assumptions.length}）`}>
              {plan.assumptions.map((a) => (
                <div key={a.field_path} className="space-y-1 rounded-lg border border-zinc-800 bg-zinc-900 p-3 text-sm">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-xs text-zinc-400">{a.field_path}</span>
                    <span className="shrink-0 rounded bg-zinc-700 px-1.5 py-0.5 text-xs text-zinc-200">
                      {KIND_LABEL[a.kind] ?? a.kind}
                    </span>
                  </div>
                  <p className="text-zinc-300">{a.rationale}</p>
                  <p className="text-xs text-zinc-500">提案值：{String(a.value)}</p>
                  {a.requires_confirmation && !a.human_confirmed ? (
                    <button
                      onClick={() => confirmAssumption(a)}
                      disabled={saving}
                      className="rounded bg-sky-600 px-2 py-1 text-xs hover:bg-sky-500 disabled:opacity-50"
                    >
                      确认此假设
                    </button>
                  ) : (
                    <span className="text-xs text-emerald-400">
                      {a.human_confirmed ? "✓ 已确认" : "无需确认（MVP 缩放/说明）"}
                    </span>
                  )}
                </div>
              ))}
            </Section>

            <Section title={`与原文偏差 Deviations（${plan.deviations_from_paper.length}）`}>
              {plan.deviations_from_paper.length === 0 && (
                <p className="text-sm text-zinc-600">（无）</p>
              )}
              {plan.deviations_from_paper.map((d) => (
                <div key={d.field_path} className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm">
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-xs text-zinc-400">{d.field_path}</span>
                    <span className="shrink-0 text-xs">
                      <span className="text-zinc-500">原文 {d.paper_value ?? "—"}</span>
                      <span className="mx-1 text-zinc-600">→</span>
                      <span className="text-violet-300">{d.plan_value}</span>
                    </span>
                  </div>
                  <p className="text-xs text-zinc-500">{d.reason}</p>
                </div>
              ))}
            </Section>

            <Section title="版本历史 Versions">
              {versions.map((v) => (
                <button
                  key={v.id}
                  onClick={() => switchVersion(v.version)}
                  className={`block w-full rounded-lg border px-3 py-2 text-left text-sm ${
                    current?.version === v.version
                      ? "border-emerald-500 bg-zinc-800"
                      : "border-zinc-800 bg-zinc-900 hover:border-zinc-600"
                  }`}
                >
                  <span className="flex items-center justify-between">
                    <span>
                      v{v.version} · {ORIGIN_LABEL[v.origin] ?? v.origin}
                    </span>
                    {v.approved_at && <span className="text-xs text-emerald-400">已批准</span>}
                  </span>
                  {v.change_reason && (
                    <span className="block text-xs text-zinc-500">{v.change_reason}</span>
                  )}
                </button>
              ))}
              {current && current.changes.length > 0 && (
                <div className="rounded-lg border border-zinc-800 bg-zinc-950 px-3 py-2 text-xs text-zinc-400">
                  <p className="mb-1 text-zinc-500">本版本改动（前 → 后）：</p>
                  {current.changes.map((c) => (
                    <p key={c.field_path}>
                      {c.field_path}: {String(c.before)} → {String(c.after)}
                    </p>
                  ))}
                  {current.related_error && (
                    <p className="mt-1 text-red-300">关联错误：{current.related_error}</p>
                  )}
                </div>
              )}
            </Section>
          </section>
        </div>
      )}
      {!plan && !error && <p className="text-sm text-zinc-500">加载中…</p>}

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
      <Suspense fallback={<div className="p-6 text-zinc-500">加载中…</div>}>
        <PlanInner experimentId={experimentId} />
      </Suspense>
    </AuthGate>
  );
}