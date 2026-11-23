"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  downloadComparisonExport,
  getComparison,
  getComparisonReport,
  rerunComparison,
} from "@/lib/api";
import type { ComparisonDetail, ComparisonRunView } from "@/lib/comparison";
import { COMPARISON_META, GROUP_LABEL } from "@/lib/comparison";
import { RUN_META, TONE_BADGE } from "@/lib/ui";

const BUSY = new Set(["queued", "running"]);

function pct(value?: number | null) {
  return value === null || value === undefined ? "—" : `${(value * 100).toFixed(2)}%`;
}

function pp(value?: number | null) {
  if (value === null || value === undefined) return "—";
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}`;
}

function RunRow({ run }: { run: ComparisonRunView }) {
  const meta = RUN_META[run.status];
  return (
    <tr>
      <td>{run.seed}</td>
      <td>{GROUP_LABEL[run.group_key]}</td>
      <td className="font-mono">{run.attempt}</td>
      <td>
        <span className={`chip ${TONE_BADGE[meta.tone]}`}>{meta.label}</span>
      </td>
      <td className="font-mono">
        {run.correct_count ?? "—"}/{run.evaluated_count ?? "—"}
      </td>
      <td className="font-mono">{pct(run.accuracy)}</td>
      <td className="max-w-[260px] truncate font-mono text-[11px] text-fg-3" title={run.error ?? ""}>
        {run.error ?? "—"}
      </td>
    </tr>
  );
}

export default function ComparisonClient({ comparisonId }: { comparisonId: string }) {
  const [detail, setDetail] = useState<ComparisonDetail | null>(null);
  const [markdown, setMarkdown] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [rerunNotice, setRerunNotice] = useState<{ id: string; warnings: string[] } | null>(null);
  const router = useRouter();

  const load = useCallback(async () => {
    try {
      const next = await getComparison(comparisonId);
      setDetail(next);
      if (next.report_status) {
        try {
          setMarkdown((await getComparisonReport(comparisonId)).markdown);
        } catch {
          setMarkdown(null);
        }
      }
      return next;
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "无法读取对照任务");
      return null;
    }
  }, [comparisonId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    if (!detail || !BUSY.has(detail.status)) return;
    const timer = setInterval(() => void load(), 2500);
    return () => clearInterval(timer);
  }, [detail, load]);

  const exportPackage = async () => {
    setBusy(true);
    setError(null);
    try {
      const blob = await downloadComparisonExport(comparisonId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `comparison-${comparisonId}.zip`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "导出失败");
    } finally {
      setBusy(false);
    }
  };

  const rerun = async (useCurrentEnvironment: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const result = await rerunComparison(comparisonId, {
        use_current_environment: useCurrentEnvironment,
        reason: useCurrentEnvironment ? "原环境不可用，改用当前环境重跑" : "按原条件重跑",
      });
      if (result.warnings?.length) {
        setRerunNotice({ id: result.comparison_id, warnings: result.warnings });
        setBusy(false);
        return;
      }
      router.push(`/comparisons?id=${result.comparison_id}`);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "重跑失败");
      setBusy(false);
    }
  };

  if (!detail) {
    return (
      <div className="mx-auto w-full max-w-5xl px-5 py-8">
        <p className="text-[13px] text-fg-3">正在读取对照任务…</p>
        {error ? <p className="mt-2 text-[12px] text-bad">{error}</p> : null}
      </div>
    );
  }

  const meta = COMPARISON_META[detail.status];
  const outcome = detail.outcome;

  return (
    <div className="mx-auto w-full max-w-5xl px-5 py-8">
      <Link href={`/papers`} className="link-quiet text-[12px]">
        ← 论文库
      </Link>
      <div className="mt-3 flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="display text-2xl text-fg">对照结果</h1>
          <p className="mt-2 max-w-2xl text-[13px] text-fg-2">{detail.question}</p>
          <p className="mt-1 text-[12px] text-fg-3">
            计划 v{detail.plan_version} · 种子 {detail.seeds.join(", ")} · 完成 {detail.completed_pairs}/
            {detail.requested_pairs} 对{detail.source_comparison_id ? " · 重跑" : ""}
          </p>
        </div>
        <span className={`chip ${TONE_BADGE[meta.tone]}`}>{meta.label}</span>
      </div>

      {error ? (
        <p role="alert" className="mt-4 rounded-md border border-bad/40 bg-bad/10 px-3 py-2 text-[12px] text-bad">
          {error}
        </p>
      ) : null}
      {rerunNotice ? (
        <div className="mt-4 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[12px] text-warn">
          <p>已按当前环境新建对照。原环境缺少或不一致：</p>
          <ul className="mt-1 space-y-0.5">
            {rerunNotice.warnings.map((warning) => (
              <li key={warning}>· {warning}</li>
            ))}
          </ul>
          <button
            className="btn btn-ghost btn-sm mt-2"
            onClick={() => router.push(`/comparisons?id=${rerunNotice.id}`)}
          >
            打开新对照
          </button>
        </div>
      ) : null}
      {detail.error ? (
        <p className="mt-4 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-[12px] text-warn">
          {detail.error}
        </p>
      ) : null}

      <section className="mt-6 grid gap-3 md:grid-cols-3">
        <div className="card p-4">
          <p className="label">这次在验证什么</p>
          <p className="mt-2 text-[12.5px] text-fg-2">
            {detail.question ?? "比较两种优化器在相同条件下的表现。"}
            两组只有优化器（和学习率）不同，其他条件完全一致。
          </p>
        </div>
        <div className="card p-4">
          <p className="label">为什么用同一批测试图片</p>
          <p className="mt-2 text-[12.5px] text-fg-2">
            如果两组看到的测试图片不同，分数差异可能来自题目难度而不是优化器。
            这里两组都用同一份 {detail.outcome?.seeds[0]?.group_a?.evaluated_count ?? "相同数量"} 张测试图片，
            并且从同一份初始权重、同一批样本顺序开始训练。
          </p>
        </div>
        <div className="card p-4">
          <p className="label">差值能说明什么</p>
          <p className="mt-2 text-[12.5px] text-fg-2">
            {outcome?.mean_delta_percentage_points !== null && outcome?.mean_delta_percentage_points !== undefined
              ? `在这套缩小规模的条件与这些随机种子下，两组准确率相差 ${pp(outcome.mean_delta_percentage_points)} 个百分点（A − B）。`
              : "只有在两组都跑完且共同条件核验通过后，才会计出差值。"}
            这是本次实验的事实，不等同于论文全量结论。
          </p>
        </div>
      </section>

      <section className="mt-6">
        <h2 className="label">配对运行</h2>
        <div className="card mt-2 overflow-x-auto p-0">
          <table className="table-lab">
            <thead>
              <tr>
                <th>种子</th>
                <th>组</th>
                <th>尝试</th>
                <th>状态</th>
                <th>正确/测试</th>
                <th>准确率</th>
                <th>错误</th>
              </tr>
            </thead>
            <tbody>
              {(detail.runs ?? []).map((run) => (
                <RunRow key={run.id} run={run} />
              ))}
              {!detail.runs.length ? (
                <tr>
                  <td colSpan={7} className="text-fg-3">尚无运行记录，任务可能仍在排队。</td>
                </tr>
              ) : null}
            </tbody>
          </table>
        </div>
        {BUSY.has(detail.status) ? (
          <p className="mt-2 text-[11.5px] text-fg-3">正在成对执行：每组失败只会补跑该组，共同条件不一致则不会计算差值。</p>
        ) : null}
      </section>

      {outcome ? (
        <section className="mt-8">
          <h2 className="label">差值与方向（由正确数/测试数重算）</h2>
          <div className="mt-2 grid gap-3 md:grid-cols-3">
            {detail.groups.map((group) => {
              const mean = group.key === "a" ? outcome.mean_a : outcome.mean_b;
              return (
                <div key={group.key} className="card p-4">
                  <p className="text-[12px] text-fg-3">{group.label} · {group.optimizer.value.toUpperCase()}</p>
                  <p className="display mt-1 text-xl text-fg">{pct(mean)}</p>
                </div>
              );
            })}
            <div className="card p-4">
              <p className="text-[12px] text-fg-3">均值差值（A − B）</p>
              <p className="display mt-1 text-xl text-fg">{pp(outcome.mean_delta_percentage_points)} pp</p>
              <p className="mt-1 text-[11.5px] text-fg-3">
                {outcome.direction === "a_higher" ? "A 组更高" : outcome.direction === "b_higher" ? "B 组更高" : outcome.direction === "equal" ? "两组相同" : "尚无方向"}
              </p>
            </div>
          </div>
          {outcome.blockers.length ? (
            <div className="card mt-3 border-warn/40 p-4">
              <p className="text-[12px] font-medium text-warn">阻塞原因</p>
              <ul className="mt-1 space-y-1 text-[12px] text-fg-2">
                {outcome.blockers.map((blocker) => (
                  <li key={blocker}>· {blocker}</li>
                ))}
              </ul>
            </div>
          ) : null}
        </section>
      ) : null}

      {detail.outcome?.verifications.length ? (
        <section className="mt-8">
          <h2 className="label">共同条件核验</h2>
          <div className="card mt-2 overflow-x-auto p-0">
            <table className="table-lab">
              <thead>
                <tr><th>种子</th><th>核验项</th><th>状态</th><th>说明</th></tr>
              </thead>
              <tbody>
                {detail.outcome.verifications.flatMap((verification) =>
                  verification.checks.map((check) => (
                    <tr key={`${verification.seed}-${check.item}`}>
                      <td>{verification.seed}</td>
                      <td className="font-mono text-[11.5px]">{check.item}</td>
                      <td>
                        <span className={`chip ${check.status === "matched" ? TONE_BADGE.ok : check.status === "unknown" ? TONE_BADGE.mute : TONE_BADGE.bad}`}>
                          {check.status}
                        </span>
                      </td>
                      <td className="text-[12px]">{check.note}</td>
                    </tr>
                  )),
                )}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}

      <section className="mt-8">
        <div className="flex flex-wrap items-center gap-3">
          <h2 className="label">对照报告</h2>
          {markdown ? (
            <>
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => {
                  const blob = new Blob([markdown], { type: "text/markdown" });
                  const url = URL.createObjectURL(blob);
                  const anchor = document.createElement("a");
                  anchor.href = url;
                  anchor.download = `comparison-${comparisonId}.md`;
                  anchor.click();
                  URL.revokeObjectURL(url);
                }}
              >
                下载 Markdown
              </button>
              <button className="btn btn-ghost btn-sm" disabled={busy} onClick={exportPackage}>
                导出产物包（zip）
              </button>
            </>
          ) : null}
        </div>
        {markdown ? (
          <pre className="terminal mt-2 max-h-[560px] overflow-auto whitespace-pre-wrap p-4 text-[12px] leading-6">
            {markdown}
          </pre>
        ) : (
          <p className="mt-2 text-[12px] text-fg-3">
            {BUSY.has(detail.status) ? "运行结束后自动生成报告。" : "暂时没有可用的报告。"}
          </p>
        )}
      </section>

      {!BUSY.has(detail.status) ? (
        <section className="mt-8 flex flex-wrap items-center gap-3 border-t border-line pt-4">
          <button className="btn btn-ghost" disabled={busy} onClick={() => void rerun(false)}>
            按原条件重跑
          </button>
          <button className="btn btn-ghost" disabled={busy} onClick={() => void rerun(true)}>
            改用当前环境另建对照
          </button>
          <span className="text-[11.5px] text-fg-3">
            重跑会新建一组运行并关联本报告；镜像不可用时会明确提示缺少项。
          </span>
        </section>
      ) : null}
    </div>
  );
}
