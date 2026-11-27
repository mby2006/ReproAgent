import type { ConditionSummaryRow, ParameterComparison } from "@/lib/types";

const SOURCES: Record<string, string> = {metrics: "运行指标", audit: "运行审计", config: "固定配置", code: "代码实现"};

function PaperPage({ page, pdfUrl }: { page: number; pdfUrl?: string | null }) {
  return pdfUrl
    ? <a className="text-info underline" href={`${pdfUrl}#page=${page}`} target="_blank" rel="noreferrer">第 {page} 页</a>
    : <span>第 {page} 页</span>;
}

function ActualFile({ path, fileHref, onFile }: { path: string; fileHref?: (path: string) => string; onFile?: (path: string) => void }) {
  if (onFile) return <button className="text-info underline" onClick={() => onFile(path)}>{path}</button>;
  if (fileHref) return <a className="text-info underline" href={fileHref(path)}>{path}</a>;
  return <code className="font-mono text-fg-3">{path}</code>;
}

export function ConditionSummary({ rows, pdfUrl, fileHref, onFile, reportVersion, executionAttempt }: {
  rows: ConditionSummaryRow[];
  pdfUrl?: string | null;
  fileHref?: (path: string) => string;
  onFile?: (path: string) => void;
  reportVersion?: number | null;
  executionAttempt?: number | null;
}) {
  return <section className="card space-y-3 p-4" aria-labelledby="condition-summary-title">
    <div className="flex flex-wrap items-start justify-between gap-3">
      <div className="min-w-0">
        <h2 id="condition-summary-title" className="display text-xl font-semibold">论文条件 / 批准计划 / 实际实现</h2>
        <p className="mt-1 text-[13px] leading-relaxed text-fg-3">先看关键条件是否一致；完整证据与影响说明保留在五列表格中。</p>
      </div>
      <div className="flex flex-wrap gap-2">
        {reportVersion != null && <span className="chip font-mono">报告 v{reportVersion}</span>}
        {executionAttempt != null && <span className="chip font-mono">最终尝试 {executionAttempt}</span>}
      </div>
    </div>
    <div className="overflow-hidden rounded-xl border border-line">
      <table className="w-full table-fixed border-collapse text-left align-top">
        <thead className="hidden md:table-header-group">
          <tr>{["项目", "论文条件", "批准计划", "实际实现"].map(h => <th scope="col" key={h}>{h}</th>)}</tr>
        </thead>
        <tbody>{rows.map(row => {
          const actual = row.actual_evidence;
          return <tr key={row.key} className="block border-b border-line-soft last:border-b-0 md:table-row">
            <th scope="row" className="block min-w-0 bg-ink-900/60 px-3 py-2 text-sm font-semibold text-fg md:table-cell md:w-[18%]">
              <span className="break-words [overflow-wrap:anywhere]">{row.label}</span>
              {row.note !== "无可用证据" && <span className="mt-1 block break-words text-xs font-normal leading-relaxed text-fg-3 [overflow-wrap:anywhere]">{String(row.note)}</span>}
            </th>
            <td data-label="论文条件" className="block min-w-0 px-3 py-2 align-top text-[13px] leading-relaxed text-fg-2 md:table-cell">
              <span className="mb-1 block text-[11px] text-fg-3 md:hidden">论文条件</span>
              <span className="break-words [overflow-wrap:anywhere]">{String(row.paper)}</span>
              {row.paper_evidence?.source_page != null && <span className="ml-1 whitespace-nowrap text-xs"><PaperPage page={row.paper_evidence.source_page} pdfUrl={pdfUrl} /></span>}
              {row.paper_evidence?.source_page == null && row.source !== "无可用证据" && <span className="ml-1 break-words text-xs text-fg-3 [overflow-wrap:anywhere]">来源：{String(row.source)}</span>}
            </td>
            <td data-label="批准计划" className="block min-w-0 px-3 py-2 align-top text-[13px] leading-relaxed text-fg-2 md:table-cell">
              <span className="mb-1 block text-[11px] text-fg-3 md:hidden">批准计划</span>
              <span className="break-words font-mono [overflow-wrap:anywhere]">{String(row.plan)}</span>
            </td>
            <td data-label="实际实现" className="block min-w-0 px-3 py-2 align-top text-[13px] leading-relaxed text-fg-2 md:table-cell">
              <span className="mb-1 block text-[11px] text-fg-3 md:hidden">实际实现</span>
              <span className={`break-words font-mono [overflow-wrap:anywhere] ${row.actual === "尚未执行" ? "text-warn" : "text-fg"}`}>{String(row.actual)}</span>
              {actual && <small className="mt-1 block text-fg-3">{SOURCES[actual.kind] ?? actual.kind} {actual.field_path}</small>}
              {actual?.file_path && <span className="mt-1 block break-all text-xs"><ActualFile path={actual.file_path} fileHref={fileHref} onFile={onFile} /></span>}
            </td>
          </tr>;
        })}</tbody>
      </table>
    </div>
  </section>;
}

export default function ParameterTable({ rows, pdfUrl, fileHref, onFile }: {
  rows: ParameterComparison[]; pdfUrl?: string | null; fileHref?: (path: string) => string; onFile?: (path: string) => void;
}) {
  return <div className="space-y-3" id="conditions">
    <h2 className="display text-xl font-semibold">逐项核查实验条件</h2>
    <div className="conditions-scroll overflow-x-auto rounded-xl border border-line" tabIndex={0} role="region" aria-label="实验条件对照表">
      <table className="table-lab w-full"><thead><tr>{["项目", "论文与出处", "实验计划", "本次实现或观测", "决定与影响"].map(h => <th scope="col" key={h}>{h}</th>)}</tr></thead>
      <tbody>{rows.map(row => {
        const paper = row.paper_evidence;
        const actual = row.actual_evidence;
        return <tr id={`condition-${row.key}`} key={row.key} className="scroll-mt-6">
          <th scope="row">{row.label}</th>
          <td data-label="论文与出处">{row.paper ?? "缺少原文证据"} {paper?.source_page && <PaperPage page={paper.source_page} pdfUrl={pdfUrl} />}
            {paper?.quote && <details><summary>引文</summary><q>{paper.quote}</q></details>}</td>
          <td data-label="实验计划">{row.plan ?? "未指定"}</td><td data-label="本次实现或观测">{row.actual ?? "未核实"}
            {actual && <small className="block text-fg-3">{SOURCES[actual.kind] ?? actual.kind} {actual.field_path}</small>}
            {actual?.file_path && onFile && <button className="text-info underline" onClick={() => onFile(actual.file_path!)}>{actual.file_path}</button>}
            {actual?.file_path && fileHref && <a className="text-info underline" href={fileHref(actual.file_path)}>{actual.file_path}</a>}
          </td><td data-label="决定与影响">{row.decision && <p>{row.decision}</p>}{row.note}</td>
        </tr>;
      })}</tbody></table>
    </div>
  </div>;
}
