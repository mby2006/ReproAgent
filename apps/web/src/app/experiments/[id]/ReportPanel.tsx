"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import type {
  CheckStatus,
  EvaluationReport,
  FinalStatus,
  RunReport,
} from "@/lib/types";

const FINAL_LABEL: Record<FinalStatus, string> = {
  METHOD_VALIDATED: "方法已验证（缩放运行）",
  SCALED_RUN_COMPLETED: "缩放运行已完成",
  PARTIAL_MATCH: "部分匹配",
  DEVIATION_FOUND: "发现偏差",
  INSUFFICIENT_EVIDENCE: "证据不足",
  EXECUTION_FAILED: "执行失败",
};

const FINAL_COLOR: Record<FinalStatus, string> = {
  METHOD_VALIDATED: "bg-emerald-700 text-emerald-100",
  SCALED_RUN_COMPLETED: "bg-sky-700 text-sky-100",
  PARTIAL_MATCH: "bg-amber-700 text-amber-50",
  DEVIATION_FOUND: "bg-red-800 text-red-100",
  INSUFFICIENT_EVIDENCE: "bg-zinc-600 text-zinc-200",
  EXECUTION_FAILED: "bg-red-800 text-red-100",
};

const CHECK_LABEL: Record<CheckStatus, string> = {
  matched: "匹配",
  deviated: "偏差",
  unknown: "未知",
  not_applicable: "不适用",
};

const CHECK_COLOR: Record<CheckStatus, string> = {
  matched: "bg-emerald-800 text-emerald-100",
  deviated: "bg-red-800 text-red-100",
  unknown: "bg-amber-800 text-amber-100",
  not_applicable: "bg-zinc-700 text-zinc-300",
};

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

function CheckBadge({ status }: { status: CheckStatus }) {
  return (
    <span className={`rounded px-1.5 py-0.5 text-xs ${CHECK_COLOR[status]}`}>
      {CHECK_LABEL[status]}
    </span>
  );
}

function fmt(value: number | null | undefined, digits = 4): string {
  return value == null ? "—" : value.toFixed(digits);
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

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1.5">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">{title}</h3>
      {children}
    </div>
  );
}

const TABLE_CLS = "w-full text-left text-xs";
const TH_CLS = "border-b border-zinc-800 px-2 py-1 text-zinc-500";
const TD_CLS = "border-b border-zinc-900 px-2 py-1 align-top text-zinc-300";

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

  return (
    <section className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-950 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-400">
          复现报告 Report
        </h2>
        {data && (
          <span className={`rounded px-2 py-0.5 text-xs ${FINAL_COLOR[data.final_status]}`}>
            {data.final_status} · {FINAL_LABEL[data.final_status]}
          </span>
        )}
        <div className="ml-auto flex gap-2">
          {report && (
            <>
              <button
                onClick={() => setShowMarkdown((v) => !v)}
                className="rounded bg-zinc-800 px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-700"
              >
                {showMarkdown ? "结构化视图" : "Markdown 原文"}
              </button>
              <button
                onClick={download}
                className="rounded bg-zinc-800 px-2 py-1 text-xs text-zinc-200 hover:bg-zinc-700"
              >
                下载 .md
              </button>
            </>
          )}
          <button
            onClick={regenerate}
            disabled={busy}
            className="rounded bg-sky-700 px-2 py-1 text-xs text-sky-100 hover:bg-sky-600 disabled:opacity-50"
          >
            {busy ? "生成中…" : report ? "重新生成" : "生成报告"}
          </button>
        </div>
      </div>

      {error && (
        <p className="rounded-lg border border-red-800 bg-red-950 px-4 py-2 text-sm text-red-300">
          {error}
        </p>
      )}
      {missing && !error && (
        <p className="text-sm text-zinc-500">报告尚未生成（运行结束后自动生成，或点击「生成报告」）。</p>
      )}

      {data && !showMarkdown && (
        <div className="space-y-4">
          <p className="text-sm text-zinc-300">{data.summary}</p>

          <Section title={`结果对比 Results Comparison（${data.metric_comparisons.length}）`}>
            <div className="overflow-x-auto rounded-lg border border-zinc-800">
              <table className={TABLE_CLS}>
                <thead>
                  <tr>
                    <th className={TH_CLS}>指标</th>
                    <th className={TH_CLS}>论文值</th>
                    <th className={TH_CLS}>实验值</th>
                    <th className={TH_CLS}>绝对差</th>
                    <th className={TH_CLS}>相对差</th>
                    <th className={TH_CLS}>百分点差</th>
                    <th className={TH_CLS}>单位</th>
                    <th className={TH_CLS}>状态</th>
                  </tr>
                </thead>
                <tbody>
                  {data.metric_comparisons.map((c) => (
                    <tr key={c.metric} title={c.note ?? undefined}>
                      <td className={TD_CLS}>{c.metric}</td>
                      <td className={TD_CLS} title={evidenceTitle(c.paper_evidence)}>
                        {fmt(c.paper_value)}
                      </td>
                      <td className={TD_CLS}>{fmt(c.experiment_value)}</td>
                      <td className={TD_CLS}>{fmt(c.absolute_difference)}</td>
                      <td className={TD_CLS}>{fmt(c.relative_difference)}</td>
                      <td className={TD_CLS}>
                        {c.percentage_point_difference == null
                          ? "—"
                          : `${c.percentage_point_difference >= 0 ? "+" : ""}${c.percentage_point_difference.toFixed(2)} pp`}
                      </td>
                      <td className={TD_CLS}>{c.unit}</td>
                      <td className={TD_CLS}>
                        <CheckBadge status={c.comparison_status} />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>

          <Section title={`方法验证 Method Validation（${data.method_checks.length}）`}>
            <div className="overflow-x-auto rounded-lg border border-zinc-800">
              <table className={TABLE_CLS}>
                <thead>
                  <tr>
                    <th className={TH_CLS}>项</th>
                    <th className={TH_CLS}>状态</th>
                    <th className={TH_CLS}>论文值</th>
                    <th className={TH_CLS}>实现值</th>
                    <th className={TH_CLS}>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {data.method_checks.map((c) => (
                    <tr key={c.item}>
                      <td className={TD_CLS}>{METHOD_ITEM_LABEL[c.item] ?? c.item}</td>
                      <td className={TD_CLS}>
                        <CheckBadge status={c.status} />
                      </td>
                      <td className={TD_CLS} title={evidenceTitle(c.paper_evidence)}>
                        {c.paper_value ?? "—"}
                      </td>
                      <td className={TD_CLS} title={evidenceTitle(c.implementation_evidence)}>
                        {c.implementation_value ?? "—"}
                      </td>
                      <td className={TD_CLS}>{c.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>

          <Section title={`实验验证 Experiment Validation（${data.experiment_checks.length}）`}>
            <div className="overflow-x-auto rounded-lg border border-zinc-800">
              <table className={TABLE_CLS}>
                <thead>
                  <tr>
                    <th className={TH_CLS}>项</th>
                    <th className={TH_CLS}>状态</th>
                    <th className={TH_CLS}>计划值</th>
                    <th className={TH_CLS}>实际值</th>
                    <th className={TH_CLS}>说明</th>
                  </tr>
                </thead>
                <tbody>
                  {data.experiment_checks.map((c) => (
                    <tr key={c.item}>
                      <td className={TD_CLS}>{EXPERIMENT_ITEM_LABEL[c.item] ?? c.item}</td>
                      <td className={TD_CLS}>
                        <CheckBadge status={c.status} />
                      </td>
                      <td className={TD_CLS}>{c.plan_value ?? "—"}</td>
                      <td className={TD_CLS} title={evidenceTitle(c.actual_evidence)}>
                        {c.actual_value ?? "—"}
                      </td>
                      <td className={TD_CLS}>{c.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </Section>

          {data.scaling_deviations.length > 0 && (
            <Section title="缩放偏差 Scaling Deviations">
              <ul className="list-disc space-y-1 pl-5 text-xs text-zinc-300">
                {data.scaling_deviations.map((d) => (
                  <li key={d.field_path}>
                    <span className="font-mono">{d.field_path}</span>：{d.paper_value ?? "—"} →{" "}
                    {d.plan_value}（{d.reason}）
                  </li>
                ))}
              </ul>
            </Section>
          )}

          {data.missing_information.length > 0 && (
            <Section title="缺失信息 Missing Information">
              <ul className="list-disc space-y-1 pl-5 text-xs text-amber-200">
                {data.missing_information.map((m, i) => (
                  <li key={i}>{m}</li>
                ))}
              </ul>
            </Section>
          )}

          {data.assumptions.length > 0 && (
            <Section title={`Agent 假设 Assumptions（${data.assumptions.length}）`}>
              <ul className="list-disc space-y-1 pl-5 text-xs text-zinc-300">
                {data.assumptions.map((a, i) => (
                  <li key={i}>
                    <span className="font-mono">{a.field_path}</span> [{a.kind}] ={" "}
                    {String(a.value)} — {a.rationale}
                    {a.human_confirmed ? "（已确认）" : ""}
                  </li>
                ))}
              </ul>
            </Section>
          )}

          <Section title="已知局限 Known Limitations">
            <ul className="list-disc space-y-1 pl-5 text-xs text-zinc-400">
              {data.limitations.map((l, i) => (
                <li key={i}>{l}</li>
              ))}
            </ul>
          </Section>

          <Section title="追溯引用 Trace References">
            <div className="grid grid-cols-2 gap-1.5 text-xs lg:grid-cols-3">
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
                <div key={label} className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2">
                  <span className="block text-zinc-500">{label}</span>
                  <span className="block break-all font-mono text-zinc-300">{value}</span>
                </div>
              ))}
            </div>
          </Section>
        </div>
      )}

      {report && showMarkdown && (
        <pre className="max-h-[32rem] overflow-auto rounded-lg border border-zinc-800 bg-black px-3 py-2 text-xs text-zinc-300">
          {report.markdown}
        </pre>
      )}
    </section>
  );
}