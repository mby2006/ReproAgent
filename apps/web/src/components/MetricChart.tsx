import type { MetricComparison } from "@/lib/types";
import { CHECK_META, TONE_BADGE, fmtNum } from "@/lib/ui";

const W = 560;
const H = 64;
const BAR_Y = 26;
const BAR_H = 14;

const STATUS_FILL: Record<string, string> = {
  matched: "var(--color-brand)",
  deviated: "var(--color-bad)",
  unknown: "var(--color-warn)",
  not_applicable: "var(--color-fg-3)",
};

function Bullet({ c }: { c: MetricComparison }) {
  const paper = c.paper_value;
  const exp = c.experiment_value;
  const tol = c.tolerance ?? 0;
  const candidates = [paper ?? 0, exp ?? 0, paper != null ? paper + tol : 0];
  const max = Math.max(...candidates, 1e-9) * 1.18;
  const x = (v: number) => Math.max(0, Math.min(W, (v / max) * W));
  const fill = STATUS_FILL[c.comparison_status] ?? "var(--color-fg-3)";
  const meta = CHECK_META[c.comparison_status];

  return (
    <div className="card space-y-1.5 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-[12px] font-semibold text-fg">{c.metric}</span>
        <span className="chip py-0!">{c.unit}</span>
        <span className={`chip ml-auto py-0! ${TONE_BADGE[meta.tone]}`}>{meta.label}</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" role="img" aria-label={`${c.metric}：论文 ${fmtNum(paper)}，实验 ${fmtNum(exp)}`}>
        <rect x="0" y={BAR_Y} width={W} height={BAR_H} rx="3" fill="var(--color-ink-700)" opacity="0.55" />
        {paper != null && tol > 0 && (
          <rect
            x={x(Math.max(0, paper - tol))}
            y={BAR_Y - 5}
            width={Math.max(2, x(Math.min(max, paper + tol)) - x(Math.max(0, paper - tol)))}
            height={BAR_H + 10}
            rx="3"
            fill="var(--color-warn)"
            opacity="0.14"
            stroke="var(--color-warn)"
            strokeOpacity="0.35"
            strokeDasharray="3 3"
            strokeWidth="0.8"
          />
        )}
        {exp != null && (
          <rect x="0" y={BAR_Y} width={Math.max(2, x(exp))} height={BAR_H} rx="3" fill={fill} opacity="0.9" />
        )}
        {paper != null && (
          <g>
            <line x1={x(paper)} x2={x(paper)} y1={BAR_Y - 9} y2={BAR_Y + BAR_H + 9} stroke="var(--color-info)" strokeWidth="2" />
            <path d={`M${x(paper) - 4} ${BAR_Y - 9} h8 l-4 5 z`} fill="var(--color-info)" />
          </g>
        )}
        {paper != null && (
          <text x={Math.min(W - 4, Math.max(30, x(paper)))} y={12} textAnchor="middle" fontSize="9.5" fontFamily="var(--font-mono)" fill="var(--color-info)">
            论文 {fmtNum(paper)}
          </text>
        )}
        {exp != null && (
          <text x={Math.min(W - 4, Math.max(34, x(exp)))} y={BAR_Y + BAR_H + 16} textAnchor="middle" fontSize="9.5" fontFamily="var(--font-mono)" fill={fill}>
            实验 {fmtNum(exp)}
          </text>
        )}
        {[0, 0.5, 1].map((t) => (
          <text key={t} x={x(max * t)} y={H - 2} textAnchor={t === 0 ? "start" : t === 1 ? "end" : "middle"} fontSize="8" fontFamily="var(--font-mono)" fill="var(--color-fg-3)">
            {(max * t).toFixed(2)}
          </text>
        ))}
      </svg>
      {c.note && <p className="text-[11px] leading-relaxed text-fg-3">{c.note}</p>}
    </div>
  );
}

export default function MetricChart({ comparisons }: { comparisons: MetricComparison[] }) {
  const usable = comparisons.filter((c) => c.paper_value != null || c.experiment_value != null);
  if (usable.length === 0) return null;
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[10px] text-fg-3">
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-0.5 bg-info" /> 论文值
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2 w-4 rounded-sm bg-brand" /> 实验值
        </span>
        <span className="flex items-center gap-1.5">
          <span className="inline-block h-2.5 w-4 rounded-sm border border-dashed border-warn/60 bg-warn/15" /> 容差带
        </span>
        <span className="ml-auto">实验值仅来自真实运行 metrics.json</span>
      </div>
      <div className="grid gap-2 lg:grid-cols-2">
        {usable.map((c) => (
          <Bullet key={c.metric} c={c} />
        ))}
      </div>
    </div>
  );
}