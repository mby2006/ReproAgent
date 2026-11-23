"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  approveComparisonPlan,
  createComparisonPlan,
  getComparisonCandidates,
  getComparisonPlan,
  reviseComparisonPlan,
  startComparison,
} from "@/lib/api";
import type {
  CandidateResponse,
  ComparisonCandidate,
  ComparisonPlanDetail,
  PendingParam,
} from "@/lib/comparison";
import { COMPARISON_META, SOURCE_LABEL } from "@/lib/comparison";
import { TONE_BADGE } from "@/lib/ui";

function sourceTone(source: string) {
  return source === "paper_explicit" ? "ok" : source === "mvp_scaling" ? "scale" : "warn";
}

function ReferenceList({ references }: { references: { label: string; quote?: string | null; source_page?: number | null }[] }) {
  if (!references.length) return <p className="text-[12px] text-warn">未定位到原文出处</p>;
  return (
    <ul className="space-y-1">
      {references.map((ref) => (
        <li key={ref.label + String(ref.source_page)} className="text-[12px] text-fg-2">
          <span className="font-mono text-fg-3">{ref.label}</span>
          {ref.quote ? <span className="quote mt-1 block">{ref.quote}</span> : null}
        </li>
      ))}
    </ul>
  );
}

function PendingRow({
  item,
  onAdopt,
  onProvide,
  busy,
}: {
  item: PendingParam;
  onAdopt: (id: string) => void;
  onProvide: (id: string, value: string) => void;
  busy: boolean;
}) {
  const [value, setValue] = useState("");
  const resolved = item.status !== "pending";
  return (
    <div className={`card p-3 ${resolved ? "opacity-70" : ""}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <p className="font-mono text-[12px] text-fg-3">{item.field_path}</p>
          <p className="text-[13px] font-medium text-fg">{item.label}</p>
        </div>
        <span className={`chip ${resolved ? TONE_BADGE.ok : TONE_BADGE.warn}`}>
          {resolved ? (item.status === "adopted" ? "已采用建议" : "已补充依据") : "待确认"}
        </span>
      </div>
      <p className="mt-2 text-[12px] text-fg-2">原因：{item.reason}</p>
      <p className="text-[12px] text-fg-2">影响：{item.impact}</p>
      {resolved ? (
        <p className="mt-2 text-[12px] text-fg-2">
          取值 <span className="font-mono text-fg">{String(item.resolved_value)}</span> ·
          依据 {item.resolved_source}
        </p>
      ) : (
        <div className="mt-3 flex flex-wrap items-end gap-2">
          {item.suggested_value !== null && item.suggested_value !== undefined ? (
            <button className="btn btn-ghost btn-sm" disabled={busy} onClick={() => onAdopt(item.id)}>
              采用建议值 {String(item.suggested_value)}
              {item.suggestion_source ? <span className="ml-1 text-fg-3">（{item.suggestion_source}）</span> : null}
            </button>
          ) : null}
          <input
            className="input w-40"
            placeholder="补充取值或依据"
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
          <button
            className="btn btn-sm btn-primary"
            disabled={busy || !value.trim()}
            onClick={() => onProvide(item.id, value.trim())}
          >
            提交依据
          </button>
        </div>
      )}
    </div>
  );
}

export default function ComparisonSetup({ paperId }: { paperId: string }) {
  const router = useRouter();
  const [candidates, setCandidates] = useState<CandidateResponse | null>(null);
  const [selected, setSelected] = useState<ComparisonCandidate | null>(null);
  const [question, setQuestion] = useState("");
  const [detail, setDetail] = useState<ComparisonPlanDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [adopt, setAdopt] = useState<string[]>([]);
  const [provided, setProvided] = useState<Record<string, string>>({});
  const [scaling, setScaling] = useState({ train: "", test: "", epochs: "" });
  const [notice, setNotice] = useState<string | null>(null);

  const loadCandidates = useCallback(async () => {
    setError(null);
    try {
      const response = await getComparisonCandidates(paperId);
      setCandidates(response);
      const first = response.candidates.find((candidate) => candidate.comparable);
      if (first) {
        setSelected(first);
        setQuestion(
          `在完全相同的训练与测试条件下，${first.group_optimizers[0].toUpperCase()} 与 ${first.group_optimizers[1].toUpperCase()} 的准确率是否存在可核验的差异？`,
        );
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "无法读取对照候选");
    }
  }, [paperId]);

  useEffect(() => {
    void loadCandidates();
  }, [loadCandidates]);

  const create = async () => {
    if (!selected) return;
    setBusy(true);
    setError(null);
    try {
      const [left, right] = selected.group_optimizers;
      const created = await createComparisonPlan(paperId, {
        question,
        groups: [
          {
            key: "a",
            label: `${left.toUpperCase()} 组`,
            optimizer: left,
            learning_rate: null,
            references: suggestedReferences(selected, left),
          },
          {
            key: "b",
            label: `${right.toUpperCase()} 组`,
            optimizer: right,
            learning_rate: null,
            references: suggestedReferences(selected, right),
          },
        ],
      });
      setDetail(created);
      setAdopt([]);
      setProvided({});
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "创建对照计划失败");
    } finally {
      setBusy(false);
    }
  };

  const refresh = async (planId: string) => {
    const latest = await getComparisonPlan(planId);
    setDetail(latest);
    setAdopt([]);
    setProvided({});
  };

  const saveVersion = async () => {
    if (!detail) return;
    setBusy(true);
    setError(null);
    try {
      const revised = await reviseComparisonPlan(detail.id, {
        base_version: detail.latest_version,
        change_reason: "确认对照参数",
        adopt_pending_ids: adopt,
        provided_pending: provided,
      });
      setDetail(revised);
      setAdopt([]);
      setProvided({});
      setNotice(`已保存为 v${revised.latest_version}`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "保存新版本失败");
    } finally {
      setBusy(false);
    }
  };

  const approve = async () => {
    if (!detail) return;
    setBusy(true);
    setError(null);
    try {
      const approved = await approveComparisonPlan(detail.id, detail.latest_version);
      setDetail(approved);
      setNotice(`v${approved.latest_version} 已批准`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "批准失败");
    } finally {
      setBusy(false);
    }
  };

  const start = async () => {
    if (!detail) return;
    setBusy(true);
    setError(null);
    try {
      const key = `cmp-${detail.id}-v${detail.latest_version}`;
      const started = await startComparison(detail.id, detail.latest_version, key);
      router.push(`/comparisons?id=${started.comparison_id}`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "启动失败");
      setBusy(false);
    }
  };

  const plan = detail?.plan;

  useEffect(() => {
    if (!detail) return;
    const base = detail.plan.base_plan;
    setScaling({
      train: String(base.dataset_config.train_subset_size),
      test: String(base.dataset_config.test_subset_size),
      epochs: String(base.training_config.epochs.value),
    });
  }, [detail]);

  const scalingDirty = Boolean(
    plan &&
      (Number(scaling.train) !== plan.base_plan.dataset_config.train_subset_size ||
        Number(scaling.test) !== plan.base_plan.dataset_config.test_subset_size ||
        Number(scaling.epochs) !== plan.base_plan.training_config.epochs.value),
  );

  const saveScaling = async () => {
    if (!detail || !plan) return;
    setBusy(true);
    setError(null);
    try {
      const revised = await reviseComparisonPlan(detail.id, {
        base_version: detail.latest_version,
        change_reason: "调整共同规模",
        train_subset_size: Number(scaling.train) || null,
        test_subset_size: Number(scaling.test) || null,
        epochs: Number(scaling.epochs) || null,
      });
      setDetail(revised);
      setNotice(`已保存为 v${revised.latest_version}，需重新批准`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "调整规模失败");
    } finally {
      setBusy(false);
    }
  };

  const openPending = plan?.pending.filter((item) => item.status === "pending") ?? [];
  const approvalReady = Boolean(plan && plan.support.ok && openPending.length === 0);
  const approved = detail?.approved_version === detail?.latest_version;

  return (
    <div className="mx-auto w-full max-w-5xl px-5 py-8">
      <Link href={`/papers?id=${paperId}`} className="link-quiet text-[12px]">
        ← 返回论文
      </Link>
      <h1 className="display mt-3 text-2xl text-fg">优化器对照</h1>
      <p className="mt-2 max-w-3xl text-[13px] text-fg-2">
        两组只在优化器（及其学习率）上不同；数据划分、初始权重、样本顺序、训练预算和评估条件必须一致。
        差值只有在两组都成功且共同条件核验通过后才会计算。
      </p>

      {error ? <p role="alert" className="mt-4 rounded-md border border-bad/40 bg-bad/10 px-3 py-2 text-[12px] text-bad">{error}</p> : null}
      {notice ? <p className="mt-4 rounded-md border border-ok/30 bg-ok/10 px-3 py-2 text-[12px] text-ok">{notice}</p> : null}

      {!detail ? (
        <section className="mt-8 space-y-4">
          <h2 className="label">1 · 论文里可对照的两组</h2>
          {!candidates ? (
            <p className="text-[13px] text-fg-3">正在从原文定位优化器与对应结果…</p>
          ) : (
            <>
              {!candidates.support.ok ? (
                <div className="card border-warn/40 p-4">
                  <p className="text-[13px] font-medium text-warn">当前模板无法回答这个问题</p>
                  <ul className="mt-2 space-y-1 text-[12px] text-fg-2">
                    {candidates.support.missing.map((gap) => (
                      <li key={gap.field}>
                        <span className="font-mono text-fg-3">{gap.field}</span> · {gap.detail}（要求：{gap.expectation}）
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
              {candidates.candidates.map((candidate) => (
                <button
                  key={candidate.id}
                  disabled={!candidate.comparable}
                  onClick={() => setSelected(candidate)}
                  className={`card card-hover w-full p-4 text-left ${selected?.id === candidate.id ? "ring-2 ring-brand" : ""} ${!candidate.comparable ? "opacity-60" : ""}`}
                >
                  <div className="flex items-center justify-between gap-3">
                    <p className="text-[14px] font-medium text-fg">{candidate.label}</p>
                    <span className={`chip ${candidate.comparable ? TONE_BADGE.ok : TONE_BADGE.bad}`}>
                      {candidate.comparable ? "可执行" : "缺少条件"}
                    </span>
                  </div>
                  <div className="mt-2"><ReferenceList references={candidate.references} /></div>
                  {candidate.note ? <p className="mt-2 text-[11.5px] text-warn">{candidate.note}</p> : null}
                  {candidate.blocking.length ? (
                    <p className="mt-2 text-[11.5px] text-bad">阻塞：{candidate.blocking.join("；")}</p>
                  ) : null}
                </button>
              ))}
              <div className="card p-4">
                <label className="label" htmlFor="comparison-question">2 · 你想验证的问题</label>
                <textarea
                  id="comparison-question"
                  className="input mt-2 h-20 w-full"
                  value={question}
                  onChange={(event) => setQuestion(event.target.value)}
                />
                <p className="mt-2 text-[11.5px] text-fg-3">
                  提到「均值/多次」时，系统会按证据用 3 个相同种子逐对运行。
                </p>
                <button className="btn btn-primary mt-3" disabled={busy || !selected || question.trim().length < 4} onClick={create}>
                  {busy ? "生成中…" : "生成对照计划"}
                </button>
              </div>
            </>
          )}
        </section>
      ) : (
        <section className="mt-8 space-y-6">
          <div className="card p-4">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div>
                <p className="label">对照计划</p>
                <p className="mt-1 text-[14px] text-fg">{plan?.question.restatement}</p>
                <p className="mt-1 text-[12px] text-fg-3">
                  版本 v{detail.latest_version} · 种子 {plan?.seeds.join(", ")} · 主指标 {plan?.question.target_metric}
                </p>
              </div>
              <div className="flex flex-col items-end gap-1">
                <span className={`chip ${TONE_BADGE[COMPARISON_META[detail.status as keyof typeof COMPARISON_META]?.tone ?? "mute"]}`}>
                  {COMPARISON_META[detail.status as keyof typeof COMPARISON_META]?.label ?? detail.status}
                </span>
                {detail.approved_version ? <span className="text-[11px] text-ok">v{detail.approved_version} 已批准</span> : null}
              </div>
            </div>
          </div>

          <div>
            <h2 className="label">共同条件（两组完全相同）</h2>
            <div className="card mt-2 overflow-x-auto p-0">
              <table className="table-lab">
                <tbody>
                  <tr><th>数据集</th><td>MNIST · 训练 {plan?.base_plan.dataset_config.train_subset_size} / 测试 {plan?.base_plan.dataset_config.test_subset_size}</td></tr>
                  <tr><th>模型</th><td>{plan?.base_plan.model_cfg.architecture}</td></tr>
                  <tr><th>损失函数</th><td>{plan?.base_plan.training_config.loss_function.value}</td></tr>
                  <tr><th>批大小 / 训练轮数</th><td>{plan?.base_plan.training_config.batch_size.value} / {plan?.base_plan.training_config.epochs.value}</td></tr>
                  <tr><th>单次预算</th><td>{plan?.base_plan.runtime_budget.max_seconds}s · {plan?.base_plan.runtime_budget.device}</td></tr>
                  <tr><th>授权范围</th><td>{plan?.authorization.note}</td></tr>
                </tbody>
              </table>
            </div>
          </div>

          <div>
            <h2 className="label">两组差异（只有优化器与学习率）</h2>
            <div className="mt-2 grid gap-3 md:grid-cols-2">
              {plan?.groups.map((group) => (
                <div key={group.key} className="card p-4">
                  <p className="text-[13px] font-medium text-fg">{group.label}</p>
                  <p className="mt-2 text-[12px] text-fg-2">
                    优化器 <span className="font-mono text-fg">{group.optimizer.value.toUpperCase()}</span>
                    <span className={`chip ml-2 ${TONE_BADGE[sourceTone(group.optimizer.source)]}`}>{SOURCE_LABEL[group.optimizer.source] ?? group.optimizer.source}</span>
                  </p>
                  <p className="mt-1 text-[12px] text-fg-2">
                    学习率 <span className="font-mono text-fg">{group.learning_rate.value}</span>
                    <span className={`chip ml-2 ${TONE_BADGE[sourceTone(group.learning_rate.source)]}`}>{SOURCE_LABEL[group.learning_rate.source] ?? group.learning_rate.source}</span>
                  </p>
                  <div className="mt-3"><ReferenceList references={group.references} /></div>
                </div>
              ))}
            </div>
          </div>

          <div>
            <h2 className="label">待确认项 {openPending.length ? `（${openPending.length}）` : "（全部已处理）"}</h2>
            <div className="mt-2 space-y-2">
              {plan?.pending.length ? (
                plan.pending.map((item) => (
                  <PendingRow
                    key={item.id}
                    item={item}
                    busy={busy}
                    onAdopt={(id) => setAdopt((current) => current.includes(id) ? current : [...current, id])}
                    onProvide={(id, value) => setProvided((current) => ({ ...current, [id]: value }))}
                  />
                ))
              ) : (
                <p className="text-[12px] text-fg-3">原文已给出两组所需参数，没有待确认项。</p>
              )}
            </div>
            {adopt.length || Object.keys(provided).length ? (
              <button className="btn btn-ghost btn-sm mt-3" disabled={busy} onClick={saveVersion}>
                保存为 v{detail.latest_version + 1}（{adopt.length + Object.keys(provided).length} 项）
              </button>
            ) : null}
          </div>

          <div>
            <h2 className="label">共同规模（改动会新建版本）</h2>
            <div className="card mt-2 p-4">
              <div className="flex flex-wrap items-end gap-3">
                <label className="text-[12px] text-fg-2">
                  训练样本
                  <input
                    className="input mt-1 w-28"
                    type="number"
                    min={1}
                    value={scaling.train}
                    onChange={(event) => setScaling({ ...scaling, train: event.target.value })}
                  />
                </label>
                <label className="text-[12px] text-fg-2">
                  测试样本
                  <input
                    className="input mt-1 w-28"
                    type="number"
                    min={1}
                    value={scaling.test}
                    onChange={(event) => setScaling({ ...scaling, test: event.target.value })}
                  />
                </label>
                <label className="text-[12px] text-fg-2">
                  训练轮数
                  <input
                    className="input mt-1 w-24"
                    type="number"
                    min={1}
                    value={scaling.epochs}
                    onChange={(event) => setScaling({ ...scaling, epochs: event.target.value })}
                  />
                </label>
                <button
                  className="btn btn-ghost btn-sm"
                  disabled={busy || !scalingDirty}
                  onClick={saveScaling}
                >
                  保存为 v{detail.latest_version + 1}
                </button>
              </div>
              <p className="mt-2 text-[11.5px] text-fg-3">
                授权上限：训练 {plan?.authorization.max_train_samples} · 测试{" "}
                {plan?.authorization.max_test_samples} · {plan?.authorization.max_epochs} epoch。
                超出上限会先生成待确认项，确认前不会执行。
              </p>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-3 border-t border-line pt-4">
            <button className="btn btn-ghost" disabled={busy} onClick={() => void refresh(detail.id)}>
              刷新状态
            </button>
            <button className="btn btn-primary" disabled={busy || !approvalReady || approved} onClick={approve}>
              {approved ? "已批准" : "批准该版本"}
            </button>
            <button className="btn btn-primary" disabled={busy || !approved} onClick={start}>
              启动成对执行
            </button>
            {!approvalReady ? (
              <span className="text-[12px] text-warn">仍有未确认的关键参数，无法批准。</span>
            ) : null}
          </div>
        </section>
      )}
    </div>
  );
}

function suggestedReferences(candidate: ComparisonCandidate, optimizer: string) {
  return candidate.references
    .filter((ref) => ref.label.toLowerCase().includes(optimizer))
    .map((ref) => ({
      label: ref.label,
      quote: ref.quote ?? null,
      source_page: ref.source_page ?? null,
      source_section: ref.source_section ?? null,
      table_id: ref.table_id ?? null,
      metric: ref.metric ?? null,
      value: ref.value ?? null,
      condition: ref.condition ?? null,
    }));
}
