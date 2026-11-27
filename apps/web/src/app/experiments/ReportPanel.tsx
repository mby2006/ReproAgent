"use client";

import ParameterTable from "@/components/ParameterTable";
import { useCallback, useEffect, useState } from "react";
import MetricChart from "@/components/MetricChart";
import { IconDownload, IconRefresh, IconWarn } from "@/components/icons";
import { apiFetch, apiHref, ApiError } from "@/lib/api";
import type {
  CheckStatus,
  EvaluationReport,
  ReportErrorCode,
  ReviewDecision,
  RunReport,
} from "@/lib/types";
import { CHECK_META, FINAL_META, TONE_BADGE, fmtDate, fmtNum } from "@/lib/ui";
import { fieldLabel, missingGloss, shortValue } from "@/lib/fieldLabels";

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
  epochs: "训练轮数",
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

function Section({
  title,
  count,
  hint,
  collapsed,
  children,
}: {
  title: string;
  count?: number;
  hint?: string;
  collapsed?: boolean;
  children: React.ReactNode;
}) {
  const heading = (
    <>
      <h3 className="label">{title}</h3>
      {count != null && <span className="chip py-0! font-mono">{count}</span>}
      <span className="h-px flex-1 bg-line-soft" />
    </>
  );
  if (collapsed) {
    return (
      <details className="group">
        <summary className="flex cursor-pointer list-none items-center gap-2 [&::-webkit-details-marker]:hidden">
          {heading}
          <span className="text-[12.5px] text-fg-3 group-open:hidden">展开</span>
          <span className="hidden text-[12.5px] text-fg-3 group-open:inline">收起</span>
        </summary>
        <div className="mt-2 space-y-2">{children}</div>
      </details>
    );
  }
  return (
    <div className="space-y-2">
      <div className="flex items-center gap-2">{heading}</div>
      {hint && <p className="text-[13px] leading-relaxed text-fg-3">{hint}</p>}
      {children}
    </div>
  );
}

function metricText(value: number | null | undefined, unit?: string | null) {
  if (value == null) return "—";
  if (unit === "fraction") return `${(value * 100).toFixed(2)}%`;
  return fmtNum(value);
}

export function FinalSummary({ data }: { data: EvaluationReport }) {
  const meta = FINAL_META[data.final_status];
  const targetName = data.target_metric ?? data.parameter_comparisons?.find(row => row.key === "target_metric")?.plan;
  const target = data.metric_comparisons.find(row => row.metric === targetName);
  const box =
    meta.tone === "ok" ? "border-ok/35 bg-ok/8"
    : meta.tone === "bad" ? "border-bad/35 bg-bad/8"
    : meta.tone === "warn" ? "border-warn/35 bg-warn/8"
    : meta.tone === "info" ? "border-info/35 bg-info/8"
    : "border-line bg-ink-900";
  return (
    <div className={`rounded-xl border px-5 py-4 ${box}`}>
      <p className="text-[12.5px] text-fg-3">结论</p>
      <p className="display mt-1 text-2xl font-semibold text-fg">{meta.label}</p>
      {data.reader_summary && <p className="mt-3 text-base leading-relaxed">{data.reader_summary}</p>}

      {target ? <>
        <p className="mt-3 text-sm">主指标 <strong>{target.metric}</strong>（{target.unit === "fraction" ? "%" : target.unit}）</p>
        <dl className="mt-4 grid grid-cols-3 gap-3">
          {[
            ["论文报告", metricText(target.paper_value, target.unit), "text-info"],
            ["本次实验", metricText(target.experiment_value, target.unit), "text-brand"],
            [
              target.percentage_point_difference == null ? "绝对差" : "百分点差",
              target.percentage_point_difference == null
                ? metricText(target.absolute_difference, target.unit)
                : `${target.percentage_point_difference >= 0 ? "+" : ""}${target.percentage_point_difference.toFixed(2)} 个百分点`,
              "text-fg",
            ],
          ].map(([label, value, tone]) => (
            <div key={label} className="rounded-lg border border-line/70 bg-ink-950/40 px-3 py-2">
              <dt className="text-[12px] text-fg-3">{label}</dt>
              <dd className={`mt-0.5 font-mono text-[15px] font-semibold ${tone}`}>{value}</dd>
            </div>
          ))}
        </dl>
      </> : <p className="mt-3 text-sm text-fg-3">
        {targetName ? `报告缺少主指标 ${targetName} 的对比记录。` : "历史报告未记录主指标。"} 可重新生成报告核查。
      </p>}

      <details className="mt-4 text-sm text-fg-2" open={!data.reader_summary}>
        <summary className="cursor-pointer">查看技术判定依据</summary>
        <p className="mt-2">{data.summary}</p>
      </details>
      <p className="mt-2 text-[13px] leading-relaxed text-fg-3">
        判定顺序：先看实验有没有跑成功，再看代码实现是否和论文一致，最后才比较数值是否落在允许范围内。
      </p>
    </div>
  );
}

const REPORT_ERROR_LABEL: Record<ReportErrorCode, string> = {
  REPORT_UNAVAILABLE: "报告尚未生成",
  REPORT_NOT_READY: "运行尚未结束，无法生成报告",
  REPORT_GENERATION_FAILED: "报告生成失败，可稍后重试",
  INVALID_REPORT_VERSION: "报告版本必须为正整数",
  REPORT_VERSION_NOT_FOUND: "所选报告版本不存在",
};

type ReportSelection = "latest" | number;

export function reportDownloadName(runId: string, version: number) {
  return `report-run-${runId}-v${version}.md`;
}

export default function ReportPanel({ runId, onFile }: { runId: string; onFile: (path: string) => void }) {
  const [report, setReport] = useState<RunReport | null>(null);
  const [selection, setSelection] = useState<ReportSelection>("latest");
  const [latestVersion, setLatestVersion] = useState<number | null>(null);
  const [versionBusy, setVersionBusy] = useState(false);
  const [missing, setMissing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ code: string; message: string } | null>(null);
  const [showMarkdown, setShowMarkdown] = useState(false);

  const load = useCallback(async (selected: ReportSelection = "latest") => {
    setError(null);
    try {
      const suffix = selected === "latest" ? "" : `?version=${selected}`;
      const r = await apiFetch<RunReport>(`/v1/runs/${runId}/report${suffix}`);
      setReport(r);
      setSelection(selected);
      setLatestVersion((current) => Math.max(current ?? 0, r.version));
      setMissing(false);
    } catch (err) {
      // 控制流只看稳定错误码；中文 message 仅用于展示。
      const code = err instanceof ApiError ? err.code : "UNKNOWN_ERROR";
      if (code === "REPORT_UNAVAILABLE") {
        setMissing(true);
        setReport(null);
        setSelection("latest");
        setLatestVersion(null);
      } else {
        setError({ code, message: err instanceof Error ? err.message : "加载报告失败" });
      }
    }
  }, [runId]);

  useEffect(() => {
    setReport(null);
    setSelection("latest");
    setLatestVersion(null);
    setMissing(false);
    setError(null);
    void load();
  }, [load]);

  async function regenerate() {
    setBusy(true);
    setError(null);
    try {
      const r = await apiFetch<RunReport>(`/v1/runs/${runId}/report`, { method: "POST" });
      setReport(r);
      setSelection("latest");
      setLatestVersion((current) => Math.max(current ?? 0, r.version));
      setMissing(false);
    } catch (err) {
      const code = err instanceof ApiError ? err.code : "UNKNOWN_ERROR";
      if (code === "REPORT_UNAVAILABLE") {
        setMissing(true);
        setReport(null);
      } else {
        setError({ code, message: err instanceof Error ? err.message : "生成报告失败" });
      }
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
    a.download = reportDownloadName(report.run_id, report.version);
    a.click();
    URL.revokeObjectURL(url);
  }

  const data: EvaluationReport | null = report?.report_json ?? null;
  const paperId = data?.references.paper_id;
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  useEffect(() => {
    if (!paperId) return;
    const controller = new AbortController();
    apiFetch<{url: string}>(`/v1/papers/${paperId}/pdf`, {signal:controller.signal})
      .then(result => { if (!controller.signal.aborted) setPdfUrl(apiHref(result.url)); })
      .catch(() => {});
    return () => controller.abort();
  }, [paperId]);
  const finalMeta = data ? FINAL_META[data.final_status] : null;

  return (
    <section className="card space-y-4 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="display text-xl font-semibold">复现报告</h2>
        {data && finalMeta && (
          <span className={`chip ${TONE_BADGE[finalMeta.tone]}`}>
            {data.final_status} · {finalMeta.label}
          </span>
        )}
        {report && (
          <span className="chip font-mono" title={`报告生成时间：${fmtDate(report.generated_at)}`}>
            报告 v{report.version} · 执行尝试 {report.execution_attempt ?? "未创建"}
          </span>
        )}
        {report && (
          <select
            aria-label="选择报告版本"
            className="input py-1.5 text-sm"
            value={selection}
            disabled={versionBusy || busy}
            onChange={(event) => {
              const value = event.target.value === "latest" ? "latest" : Number(event.target.value);
              setVersionBusy(true);
              void load(value).finally(() => setVersionBusy(false));
            }}
          >
            <option value="latest">最新版本{latestVersion ? ` (v${latestVersion})` : ""}</option>
            {Array.from({ length: latestVersion ?? report.version }, (_, index) => index + 1)
              .reverse().map((version) => (
                <option key={version} value={version}>v{version}</option>
              ))}
          </select>
        )}
        <div className="ml-auto flex flex-wrap gap-2">
          {report && (
            <>
              <button aria-pressed={showMarkdown} onClick={() => setShowMarkdown((v) => !v)} className="btn btn-ghost btn-sm">
                {showMarkdown ? "结构化视图" : "Markdown 原文"}
              </button>
              <button onClick={download} className="btn btn-ghost btn-sm">
                <IconDownload className="h-3.5 w-3.5" />
                下载报告
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
        <p role="alert" className="flex items-start gap-2 rounded-lg border border-bad/30 bg-bad/8 px-4 py-2 text-sm text-bad">
          <IconWarn className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            {/* 展示按稳定码选型，不解析 message 文本 */}
            {REPORT_ERROR_LABEL[error.code as ReportErrorCode] ?? `报告请求失败（${error.code}）`}
            {error.message && error.message !== REPORT_ERROR_LABEL[error.code as ReportErrorCode] && (
              <span className="ml-1 text-fg-3">— {error.message}</span>
            )}
          </span>
        </p>
      )}
      {missing && !error && (
        <p className="text-sm text-fg-3">报告尚未生成（运行结束后自动生成，或点击「生成报告」）。</p>
      )}

      {data && <ReportReview data={data} />}

      {data && finalMeta && !showMarkdown && (
        <div className="space-y-5">
          <FinalSummary data={data} />
          {!!data.parameter_comparisons?.length && <ParameterTable rows={data.parameter_comparisons} pdfUrl={pdfUrl} onFile={onFile}/>}

          <Section
            title="关键指标：论文 vs 本次"
            count={data.metric_comparisons.length}
            hint="“论文报告”来自论文原文（附页码证据），“本次实验”来自本次运行日志。“百分点差”比相对百分比更直观：99% 降到 90% 是差 9 个百分点。"
          >
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
                        {metricText(c.paper_value, c.unit)}
                      </td>
                      <td className="font-mono text-brand">
                        {metricText(c.experiment_value, c.unit)}
                      </td>
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

          <Section
            title="代码实现是否和论文一致"
            count={data.method_checks.length}
            hint="逐项比较论文写的和代码里真正实现的。“论文没写清”表示论文未给出该细节，无法判断对错。"
            collapsed
          >
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

          <Section
            title="计划要跑的 vs 实际跑的"
            count={data.experiment_checks.length}
            hint="确认实际训练规模、优化器等与批准的计划完全一致，防止“说一套跑一套”。"
            collapsed
          >
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
            <Section
            title="为跑得动而缩小的部分"
            hint="这些调整减少了运行时间，也改变了比较条件。单次运行无法判断每项调整对成绩的影响。"
          >
              <ul className="space-y-1.5">
                {data.scaling_deviations.map((d) => (
                  <li key={d.field_path} className="rounded-lg border border-scale/25 bg-scale/6 px-3 py-2 text-xs text-fg-2">
                    <span className="text-[13px] font-medium text-scale">{fieldLabel(d.field_path)}</span>
                    <span className="mx-1.5 font-mono">{d.paper_value ?? "—"}</span>→
                    <span className="ml-1.5 font-mono text-fg">{d.plan_value}</span>
                    <span className="ml-2 text-fg-3">（{d.reason}）</span>
                  </li>
                ))}
              </ul>
            </Section>
          )}

          {data.missing_information.length > 0 && (
            <Section
            title="论文没写清的地方"
            hint="论文常见“只给结论不给细节”，这些空缺只能靠假设补齐，也是结果不确定的主要来源。"
          >
              <ul className="space-y-1.5">
                {data.missing_information.map((m, i) => (
                  <li key={i} className="rounded-lg border border-warn/25 bg-warn/6 px-3 py-2">
                    <p className="flex items-start gap-2 text-[13px] text-warn">
                      <IconWarn className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                      <span className="break-words">{shortValue(m, 110)}</span>
                    </p>
                    <p className="mt-1 pl-5 text-[12.5px] leading-relaxed text-fg-3">
                      {missingGloss(m)}
                    </p>
                  </li>
                ))}
              </ul>
            </Section>
          )}

          {data.assumptions.length > 0 && (
            <Section
            title="系统补上的假设"
            count={data.assumptions.length}
            hint="论文没写、由系统补上的取值。右侧标签说明它是人工确认、AI 建议后采纳，还是系统自动接受。"
            collapsed
          >
              <ul className="space-y-1.5">
                {data.assumptions.map((a, i) => (
                  <li key={i} className="rounded-lg border border-line bg-ink-900 px-3 py-2 text-xs text-fg-2">
                    <span className="text-[13px] font-medium text-fg">{fieldLabel(a.field_path)}</span>
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

          <Section
            title="谁在什么时候改了什么（已脱敏）"
            count={(data.review_decisions ?? []).length}
            collapsed
          >
            <ReviewAudit decisions={data.review_decisions ?? []} />
          </Section>

          <Section
            title="这份报告的限制"
            hint="这些限制决定了结论能被用到什么程度，请和上面的结论一起读。"
          >
            <ul className="space-y-1 text-xs leading-relaxed text-fg-3">
              {data.limitations.map((l, i) => (
                <li key={i} className="flex gap-2">
                  <span className="text-fg-3">·</span>
                  {l}
                </li>
              ))}
            </ul>
          </Section>

          <Section title="技术追溯信息" collapsed>
            <div className="grid grid-cols-1 gap-1.5 text-xs sm:grid-cols-2 lg:grid-cols-4">
              {[
                ["Paper ID", data.references.paper_id],
                ["Spec 版本", `v${data.references.spec_version} · ${data.references.spec_version_id}`],
                ["Experiment ID", data.references.experiment_id],
                ["Plan 版本", `v${data.references.plan_version} · ${data.references.plan_id}`],
                ["Code 版本", `v${data.references.code_version}`],
                ["Code Hash", data.references.code_hash ?? "—"],
                ["Run ID", data.references.run_id],
                ["Workflow ID", data.references.workflow_id ?? "—"],
                ["执行尝试", data.references.execution_attempt ?? "未创建"],
                ["Runner Request ID", data.references.execution_request_id ?? "—"],
                ["Runner Run ID", data.references.runner_run_id ?? "—"],
                ["报告生成耗时", report?.generation_ms == null ? "—" : `${report.generation_ms} ms`],
                ["固定代码文件", `${data.references.generated_file_ids.length} 个`],
              ].map(([label, value]) => (
                <div key={label} className="rounded-lg border border-line bg-ink-900 px-3 py-2">
                  <span className="label block text-[11px]!">{label}</span>
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
            <span className="font-mono text-[12px] text-fg-3">{reportDownloadName(report.run_id, report.version)}</span>
          </div>
          <pre className="max-h-[32rem] overflow-auto px-4 py-3 text-xs leading-relaxed text-fg-2">
            {report.markdown}
          </pre>
        </div>
      )}
    </section>
  );
}
