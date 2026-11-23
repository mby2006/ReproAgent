import type { ParameterComparison } from "@/lib/types";

const SOURCES: Record<string, string> = {metrics: "运行指标", audit: "运行审计", config: "固定配置", code: "代码实现"};

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
          <td data-label="论文与出处">{row.paper ?? "缺少原文证据"} {paper?.source_page && (pdfUrl
            ? <a className="text-info underline" href={`${pdfUrl}#page=${paper.source_page}`} target="_blank" rel="noreferrer">第 {paper.source_page} 页</a>
            : <span>第 {paper.source_page} 页</span>)}
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
