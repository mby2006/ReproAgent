"use client";

import { useCallback, useEffect, useState } from "react";
import MetricChart from "@/components/MetricChart";
import { IconDownload, IconRefresh, IconWarn } from "@/components/icons";
import { apiFetch } from "@/lib/api";
import type {
  CheckStatus,
  EvaluationReport,
  ReviewDecision,
  RunReport,
} from "@/lib/types";
import { CHECK_META, FINAL_META, TONE_BADGE, fmtNum } from "@/lib/ui";

const METHOD_ITEM_LABEL: Record<string, string> = {
  architecture: "架构",
  layer_configuration: "层配置",
  activation: "激活函数",
  subsampling: "下采样",
  output_layer: "输出层",
  loss_function: "损失函数",
  preprocessing: "预处理",
  dataset: "数据集",
};

const EXPERIMENT_ITEM_LABEL: Record<string, string> = {
  training_samples: "训练样本数",
  test_samples: "测试样本数",
  epochs: "Epochs",
  batch_size: "批大小",
  optimizer: "优化器",
  learning_rate: "学习率",
  random_seed: "随机种子",
  dataset_fingerprint: "数据集指纹",
  runtime_environment: "运行环境",
};

const REVIEW_LABEL: Record<string, string> = {
  expert: "人工逐项核验", assisted: "AI 辅助审核", delegated: "托管自动（未逐项核验）",
};
const SOURCE_LABEL: Record<string, string> = {
  human: "human · 人工确认",
  ai_assisted: "ai_assisted · AI 辅助，人工采纳",
  auto: "auto · 系统自动接受，非人工确认",
};
const DELEGATED_DISCLAIMER = "本实验关键假设由系统自动接受，未经人工逐项核验；结果仅供趋势参考。";

function sourceLabel(source?: string | null, confirmed = false) {
  return (source && SOURCE_LABEL[source]) || (confirmed ? "unknown · 旧记录来源未知" : "未确认");
}

function auditValue(value: unknown) {
  return value == null ? "—" : typeof value === "string" ? value : JSON.stringify(value);
}

export function ReportReview({ data }: { data: EvaluationReport }) {
  const automatic = data.review_mode === "delegated"
    || data.assumptions.some((a) => a.confirmed_by === "auto")
    || (data.review_decisions ?? []).some((d) => d.confirmed_by === "auto");
  const disclaimer = automatic ? DELEGATED_DISCLAIMER : data.review_disclaimer
    || "审核来源未知（unknown）：缺少审核记录，不能认定已人工逐项核验。";
  return (
    <aside aria-label="审核来源声明" className="space-y-2 rounded-lg border border-warn/30 bg-warn/8 px-4 py-3">
      <span className="chip badge-warn">{REVIEW_LABEL[data.review_mode ?? ""] ?? "审核来源未知 · unknown"}</span>
      <p className="text-sm font-medium text-warn">{disclaimer}</p>
      {data.delegated_by && data.delegated_at && (
        <p className="break-all text-xs text-fg-2">
          托管授权用户：{data.delegated_by} · 授权时间：<time dateTime={data.delegated_at}>{data.delegated_at}</time>
        </p>
      )}
      <p className="text-xs text-fg-3">确认来源不等于论文原文证据；自动接受与 AI 辅助采纳仍保留推断属性。</p>
    </aside>
  );
}

export function ReviewAudit({ decisions }: { decisions: ReviewDecision[] }) {
  return (
    <div className="space-y-2">
      {decisions.length === 0 && <p className="text-xs text-fg-3">无持久审核事件；旧记录来源未知。</p>}
      {decisions.map((d) => (
        <details key={d.id} id={`review-${d.id}`} className="scroll-mt-4 rounded-lg border border-line bg-ink-900 p-3 open:border-info/40">
          <summary className="cursor-pointer break-all text-xs text-fg-2">
            <span className="font-mono text-info">{d.field_path}</span>
            <span className="chip mx-2 py-0!">{sourceLabel(d.confirmed_by)}</span>
            <span className="font-mono">{d.id}</span>
          </summary>
          <dl className="mt-3 grid gap-2 break-all text-xs sm:grid-cols-2">
            {[
              ["旧值（已脱敏）", auditValue(d.before)], ["新值（已脱敏）", auditValue(d.after)],
              ["审核模式", REVIEW_LABEL[d.review_mode ?? ""] ?? "unknown"],
              ["操作用户", d.actor ?? "unknown"], ["时间", d.timestamp ?? "unknown"],
              ["推荐 ID", d.recommendation_id ?? "—"],
              ["推荐置信度", d.recommendation_confidence ?? "—"],
              ["推荐风险", d.recommendation_risk ?? "—"],
              ["预选阈值", d.preselection_threshold ?? "—"],
              ["推荐理由（已脱敏）", d.recommendation_reasoning ?? "—"],
            ].map(([label, value]) => (
              <div key={label}><dt className="text-fg-3">{label}</dt><dd className="mt-1 whitespace-pre-wrap text-fg-2">{value}</dd></div>
            ))}
          </dl>
        </details>
      ))}
    </div>
  );
}

function CheckBadge({ status }: { status: CheckStatus }) {
  const meta = CHECK_META[status];
  return <span className={`chip py-0! ${TONE_BADGE[meta.tone]}`}>{meta.label}</span>;
}

function evidenceTitle(ref: { source_page?: number | null; quote?: string | null; file_path?: string | null; field_path?: string | null } | null): string {
  if (!ref) return "无证据";
  const parts: string[] = [];
  if (ref.source_page) parts.push(`p.${ref.source_page}`);
  if (ref.file_path) parts.push(ref.file_path);
  if (ref.field_path) parts.push(ref.field_path);
  if (ref.quote) parts.push(ref.quote);
  return parts.join(" · ") || "无证据";
}

function Section({ title, count, children }: { title: string; count?: number; children: React.ReactNode }) {
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">
        <h3 className="label">{title}</h3>
        {count != null && <span className="chip py-0! font-mono">{count}</span>}
        <span className="h-px flex-1 bg-line-soft" />
      </div>
      {children}
    </div>
  );
}

export default function ReportPanel({ runId }: { runId: string }) {
  const [report, setReport] = useState<RunReport | null>(null);
  const [missing, setMissing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showMarkdown, setShowMarkdown] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await apiFetch<RunReport>(`/v1/runs/${runId}/report`);
      setReport(r);
      setMissing(false);
    } catch (err) {
      const message = err instanceof Error ? err.message : "";
      if (message.includes("尚未生成")) {
        setMissing(true);
        setReport(null);
      } else {
        setError(message || "加载报告失败");
      }
    }
  }, [runId]);

  useEffect(() => {
    setReport(null);
    setMissing(false);
    setError(null);
    load();
  }, [load]);

  async function regenerate() {
    setBusy(true);
    setError(null);
    try {
      const r = await apiFetch<RunReport>(`/v1/runs/${runId}/report`, { method: "POST" });
      setReport(r);
      setMissing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "生成报告失败");
    } finally {
      setBusy(false);
    }
  }

  function download() {
    if (!report) return;
    const blob = new Blob([report.markdown], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `reproduction-report-${runId.slice(0, 8)}.md`;
    a.click();
    URL.revokeObjectURL(url);
  }

  const data: EvaluationReport | null = report?.report_json ?? null;
  const finalMeta = data ? FINAL_META[data.final_status] : null;

  return (
    <section className="card space-y-4 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="label">复现报告 Report</h2>
        {data && finalMeta && (
          <span className={`chip ${TONE_BADGE[finalMeta.tone]}`}>
            {data.final_status} · {finalMeta.label}
          </span>
        )}
        <div className="ml-auto flex flex-wrap gap-2">
          {report && (
            <>
              <button onClick={() => setShowMarkdown((v) => !v)} className="btn btn-ghost btn-sm">
                {showMarkdown ? "结构化视图" : "Markdown 原文"}
              </button>
              <button onClick={download} className="btn btn-ghost btn-sm">
                <IconDownload className="h-3.5 w-3.5" />
                下载 .md
              </button>
            </>
          )}
          <button onClick={regenerate} disabled={busy} className="btn btn-info btn-sm">
            <IconRefresh className={`h-3.5 w-3.5 ${busy ? "animate-spin" : ""}`} />
            {busy ? "生成中…" : report ? "重新生成" : "生成报告"}
          </button>
        </div>
      </div>

      {error && (
        <p className="flex items-start gap-2 rounded-lg border border-bad/30 bg-bad/8 px-4 py-2 text-sm text-bad">
          <IconWarn className="mt-0.5 h-4 w-4 shrink-0" />
          {error}
        </p>
      )}
      {missing && !error && (
        <p className="text-sm text-fg-3">报告尚未生成（运行结束后自动生成，或点击「生成报告」）。</p>
      )}

      {data && <ReportReview data={data} />}

      {data && finalMeta && !showMarkdown && (
        <div className="space-y-5">
          <div className={`rounded-lg border px-4 py-3 ${
            finalMeta.tone === "ok" ? "border-ok/30 bg-ok/8"
            : finalMeta.tone === "bad" ? "border-bad/30 bg-bad/8"
            : finalMeta.tone === "warn" ? "border-warn/30 bg-warn/8"
            : finalMeta.tone === "info" ? "border-info/30 bg-info/8"
            : "border-line bg-ink-900"
          }`}>
            <p className="display text-lg font-semibold text-fg">{finalMeta.label}</p>
            <p className="mt-1 text-sm leading-relaxed text-fg-2">{data.summary}</p>
          </div>

          <Section title="结果对比 Results Comparison" count={data.metric_comparisons.length}>
            <MetricChart comparisons={data.metric_comparisons} />
            <div className="overflow-x-auto rounded-lg border border-line">
              <table className="table-lab">
                <thead>
                  <tr>
                    <th>指标</th>
                    <th>论文值</th>
                    <th>实验值</th>
                    <th>绝对差</th>
                    <th>相对差</th>
                    <th>百分点差</th>
                    <th>单位</th>
                    <th>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {data.metric_comparisons.map((c) => (
                    <tr key={c.metric} title={c.note ?? undefined}>
                      <td className="font-mono">{c.metric}</td>
                      <td className="font-mono text-info" title={evidenceTitle(c.paper_evidence)}>
                        {fmtNum(c.paper_value)}
                      </td>
                      <td className="font-mono text-brand">{fmtNum(c.experiment_value)}</td>
                      <td className="font-mono">{fmtNum(c.absolute_difference)}</td>
                      <td className="font-mono">{fmtNum(c.relative_difference)}</td>
                      <td className="font-mono">
                        {c.percentage_point_difference == null
                          ? "—"
                          : `${c.percentage_point_difference >= 0 ? "+" : ""}${c.percentage_point_difference.toFixed(2)} pp`}
                      </td>
                      <td>{c.unit}</td>
                      <td>
                        <CheckBadge status={c.comparison_status} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>

          <Section title="方法验证 Method Validation" count={data.method_checks.length}>
            <div className="overflow-x-auto rounded-lg border border-line">
              <table className="table-lab">
                <thead>
                  <tr>
                    <th>项</th>
                    <th>状态</th>
                    <th>论文值</th>
                    <th>实现值</th>
                    <th>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {data.method_checks.map((c) => (
                    <tr key={c.item}>
                      <td>{METHOD_ITEM_LABEL[c.item] ?? c.item}</td>
                      <td>
                        <CheckBadge status={c.status} />
                      </td>
                      <td className="max-w-56 truncate" title={evidenceTitle(c.paper_evidence)}>
                        {c.paper_value ?? "—"}
                      </td>
                      <td className="max-w-56 truncate font-mono" title={evidenceTitle(c.implementation_evidence)}>
                        {c.implementation_value ?? "—"}
                      </td>
                      <td className="text-fg-2">{c.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>

          <Section title="实验验证 Experiment Validation" count={data.experiment_checks.length}>
            <div className="overflow-x-auto rounded-lg border border-line">
              <table className="table-lab">
                <thead>
                  <tr>
                    <th>项</th>
                    <th>状态</th>
                    <th>计划值</th>
                    <th>实际值</th>
                    <th>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {data.experiment_checks.map((c) => (
                    <tr key={c.item}>
                      <td>{EXPERIMENT_ITEM_LABEL[c.item] ?? c.item}</td>
                      <td>
                        <CheckBadge status={c.status} />
                      </td>
                      <td className="max-w-48 truncate font-mono">{c.plan_value ?? "—"}</td>
                      <td className="max-w-48 truncate font-mono" title={evidenceTitle(c.actual_evidence)}>
                        {c.actual_value ?? "—"}
                      </td>
                      <td className="text-fg-2">{c.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>

          {data.scaling_deviations.length > 0 && (
            <Section title="缩放偏差 Scaling Deviations">
              <ul className="space-y-1.5">
                {data.scaling_deviations.map((d) => (
                  <li key={d.field_path} className="rounded-lg border border-scale/25 bg-scale/6 px-3 py-2 text-xs text-fg-2">
                    <span className="font-mono text-scale">{d.field_path}</span>
                    <span className="mx-1.5 font-mono">{d.paper_value ?? "—"}</span>→
                    <span className="ml-1.5 font-mono text-fg">{d.plan_value}</span>
                    <span className="ml-2 text-fg-3">（{d.reason}）</span>
                  </li>
                ))}
              </ul>
            </Section>
          )}

          {data.missing_information.length > 0 && (
            <Section title="缺失信息 Missing Information">
              <ul className="space-y-1.5">
                {data.missing_information.map((m, i) => (
                  <li key={i} className="flex items-start gap-2 rounded-lg border border-warn/25 bg-warn/6 px-3 py-2 text-xs text-warn">
                    <IconWarn className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    {m}
                  </li>
                ))}
              </ul>
            </Section>
          )}

          {data.assumptions.length > 0 && (
            <Section title="Agent 假设 Assumptions" count={data.assumptions.length}>
              <ul className="space-y-1.5">
                {data.assumptions.map((a, i) => (
                  <li key={i} className="rounded-lg border border-line bg-ink-900 px-3 py-2 text-xs text-fg-2">
                    <span className="font-mono text-info">{a.field_path}</span>
                    <span className="chip mx-1.5 py-0!">{a.kind}</span>=
                    <span className="ml-1.5 font-mono text-fg">{String(a.value)}</span>
                    <span className="ml-2 text-fg-3">— {a.rationale}</span>
                    <span className={`chip ml-2 py-0! ${a.confirmed_by === "auto" ? "badge-warn" : "badge-info"}`}>
                      {sourceLabel(a.confirmed_by, a.human_confirmed)}
                    </span>
                  </li>
                ))}
              </ul>
            </Section>
          )}

          <Section title="审核 Trace（已脱敏）" count={(data.review_decisions ?? []).length}>
            <ReviewAudit decisions={data.review_decisions ?? []} />
          </Section>

          <Section title="已知局限 Known Limitations">
            <ul className="space-y-1 text-xs leading-relaxed text-fg-3">
              {data.limitations.map((l, i) => (
                <li key={i} className="flex gap-2">
                  <span className="text-fg-3">·</span>
                  {l}
                </li>
              ))}
            </ul>
          </Section>

          <Section title="追溯引用 Trace References">
            <div className="grid grid-cols-1 gap-1.5 text-xs sm:grid-cols-2 lg:grid-cols-4">
              {[
                ["Paper ID", data.references.paper_id],
                ["Spec 版本", `v${data.references.spec_version} · ${data.references.spec_version_id}`],
                ["Experiment ID", data.references.experiment_id],
                ["Plan 版本", `v${data.references.plan_version} · ${data.references.plan_id}`],
                ["Code 版本", `v${data.references.code_version}`],
                ["Code Hash", data.references.code_hash ?? "—"],
                ["Run ID", data.references.run_id],
                ["固定代码文件", `${data.references.generated_file_ids.length} 个`],
              ].map(([label, value]) => (
                <div key={label} className="rounded-lg border border-line bg-ink-900 px-3 py-2">
                  <span className="label block text-[9px]!">{label}</span>
                  <span className="mt-0.5 block break-all font-mono text-fg-2">{value}</span>
                </div>
              ))}
            </div>
            <div className="space-y-1 break-all text-xs">
              <p className="label">审核事件 ID</p>
              {(data.references.review_trace_ids ?? []).map((id) => (
                <a key={id} href={`#review-${id}`} className="block font-mono text-info underline" onClick={() => {
                  const target = document.getElementById(`review-${id}`);
                  if (target instanceof HTMLDetailsElement) target.open = true;
                }}>{id}</a>
              ))}
            </div>
          </Section>
        </div>
      )}

      {report && showMarkdown && (
        <div className="terminal">
          <div className="terminal-bar">
            <span className="terminal-dot bg-bad/70" />
            <span className="terminal-dot bg-warn/70" />
            <span className="terminal-dot bg-ok/70" />
            <span className="font-mono text-[11px] text-fg-3">reproduction-report-{runId.slice(0, 8)}.md</span>
          </div>
          <pre className="max-h-[32rem] overflow-auto px-4 py-3 text-xs leading-relaxed text-fg-2">
            {report.markdown}
          </pre>
        </div>
      )}
    </section>
  );
}