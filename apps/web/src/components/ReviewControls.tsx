"use client";

import { useEffect, useId, useRef, useState } from "react";
import type { ConfirmationSource, FieldExplanation, FieldRecommendation, OrdinaryReviewMode, ReviewMode } from "@/lib/types";

export function ReviewModeSelector({ mode, onChange, onDelegate, disabled, delegationDisabled }: {
  mode: ReviewMode;
  onChange: (mode: OrdinaryReviewMode) => void;
  onDelegate: () => void;
  disabled: boolean;
  delegationDisabled: boolean;
}) {
  return (
    <div className="card flex flex-wrap items-center gap-3 p-4">
      <label className="flex items-center gap-2 text-[14px] text-fg">
        这些设置谁来定
        <select className="select" value={mode} disabled={disabled}
          onChange={(event) => {
            if (event.target.value === "delegated") {
              // Restore the controlled value before opening consent; cancellation changes no mode.
              event.target.value = mode;
              if (!delegationDisabled) onDelegate();
            } else {
              onChange(event.target.value as OrdinaryReviewMode);
            }
          }}>
          <option value="assisted">AI 给建议，我来确认（默认）</option>
          <option value="expert">每一项都由我确认</option>
          <option value="delegated" disabled={delegationDisabled}>全部交给系统（需单独授权）</option>
        </select>
      </label>
      <p className="text-[13px] leading-relaxed text-fg-3">
        这里只是切换页面的建议方式，真正生效要等你保存或批准。
        选择“全部交给系统”会先弹窗单独授权，不会自动提交。
        {mode === "delegated" && " 如果之后手动改动了任何一项，原授权会失效，需要重新确认。"}
      </p>
    </div>
  );
}

const DELEGATION_STATEMENT = "系统会自动接受全部 AI 建议和安全默认值，关键设置不会经过人工逐项核验；结果只能当趋势参考；授权只对本次版本有效，手动改动后需要重新授权";

// Mount only while requesting consent. Native dialog supplies focus trapping and Escape handling.
export function DelegationDialog({ version, workflowDriven, budget, onConfirm, onCancel, onExpert }: {
  version: number;
  workflowDriven: boolean;
  budget?: string;
  onConfirm: () => Promise<void>;
  onCancel: () => void;
  onExpert: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancelButton = useRef<HTMLButtonElement>(null);
  const inFlight = useRef(false);
  const titleId = useId();
  const descriptionId = useId();
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const element = dialog.current;
    const trigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    element?.showModal();
    cancelButton.current?.focus();
    return () => {
      element?.close();
      trigger?.focus();
    };
  }, []);

  async function confirm() {
    if (!consent || inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await onConfirm();
    } catch (err) {
      setError(err instanceof Error ? err.message : "授权失败，请显式重试或改用专家模式");
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  }

  return (
    <dialog ref={dialog} aria-labelledby={titleId} aria-describedby={descriptionId} aria-busy={busy}
      onCancel={(event) => {
        event.preventDefault();
        if (!inFlight.current) onCancel();
      }}
      className="card fixed inset-0 m-auto max-h-[90dvh] w-[calc(100%-2rem)] max-w-lg overflow-y-auto p-6 text-fg backdrop:bg-black/70">
      <h2 id={titleId} className="display text-lg font-semibold">授权托管 v{version}？</h2>
      <div id={descriptionId} className="mt-3 space-y-3 text-sm leading-relaxed text-fg-2">
        <p className="rounded-lg border border-warn/40 bg-warn/10 p-3 text-warn">{DELEGATION_STATEMENT}。</p>
        <p>确认将创建新的不可变版本，保留原版本。自动接受不等于人工确认。</p>
        {workflowDriven
          ? <p>批准后按当前预算自动执行{budget ? `（${budget}）` : ""}；工作流将自动生成建议、接受、批准并执行，无需后续逐项点击。</p>
          : <p>此次仅保存确认 / 批准，不会自动启动运行。</p>}
      </div>
      <label className="mt-4 flex items-start gap-2 text-sm text-fg">
        <input type="checkbox" checked={consent} disabled={busy} required
          onChange={(event) => setConsent(event.target.checked)} aria-describedby={descriptionId}
          className="mt-0.5 h-4 w-4 shrink-0 accent-[var(--color-brand)] focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand" />
        我已了解以上声明，并同意本次版本及其工作流的托管授权。
      </label>
      {error && <div role="alert" className="mt-4 space-y-2 rounded-lg border border-bad/40 bg-bad/10 p-3 text-sm text-bad">
        <p className="break-words">授权未完成：{error}</p>
        <p>不会自动重试。可重试授权，或取消后加载最新版本核对。</p>
        <button type="button" className="btn btn-ghost btn-sm" disabled={busy} onClick={() => { if (!inFlight.current) onExpert(); }}>改用专家模式，人工审核</button>
      </div>}
      <div className="mt-6 flex flex-wrap justify-end gap-2">
        <button ref={cancelButton} type="button" autoFocus disabled={busy} onClick={() => { if (!inFlight.current) onCancel(); }} className="btn btn-ghost btn-sm">取消</button>
        <button type="button" disabled={!consent || busy} onClick={confirm} className="btn btn-primary btn-sm">
          {busy ? "授权中…" : error ? "重试授权" : "确认授权托管"}
        </button>
      </div>
    </dialog>
  );
}

export function DelegationNotice({ delegatedAt, delegatedBy }: {
  delegatedAt: string | null;
  delegatedBy: string | null;
}) {
  return (
    <div role="status" className="space-y-2 rounded-lg border border-warn/40 bg-warn/10 px-4 py-3 text-sm text-warn">
      <p className="font-medium">该版本由系统自动批准，没有人逐项看过</p>
      <p className="leading-relaxed">{DELEGATION_STATEMENT}。</p>
      <p className="break-words text-xs">授权人：{delegatedBy ?? "未记录（授权无效）"} · 授权时间：{delegatedAt ?? "未记录（授权无效）"}</p>
    </div>
  );
}

export function ConfirmationBadge({ by }: { by?: ConfirmationSource | null }) {
  if (!by) return null;
  if (by === "auto") return <span className="chip badge-warn">系统自动接受 · 未人工核验</span>;
  return <span className="chip badge-info">{by === "ai_assisted" ? "AI 建议 · 人工采纳" : "人工确认"}</span>;
}

export function ExplanationCard({ explanation, mode }: { explanation: FieldExplanation; mode: ReviewMode }) {
  return (
    <details key={mode} open={mode === "assisted"} className="rounded-lg border border-info/25 bg-info/5 p-3">
      <summary className="cursor-pointer text-xs font-medium text-info focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand">
        AI 通俗解读，非论文原文
      </summary>
      <dl className="mt-3 space-y-2 text-xs leading-relaxed">
        {([
          ["这是什么", explanation.what], ["为什么重要", explanation.why],
          ["接受会怎样", explanation.consequence], ["风险", explanation.risk],
        ] as const).map(([label, text]) => (
          <div key={label}>
            <dt className="font-medium text-fg">{label}</dt>
            <dd className="mt-0.5 whitespace-pre-wrap break-words text-fg-2">{text}</dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

export function ReviewRequestStatus({ label, loading, error, loaded, disabled, onLoad, onExpert }: {
  label: string;
  loading: boolean;
  error: string | null;
  loaded: boolean;
  disabled: boolean;
  onLoad: () => void;
  onExpert: () => void;
}) {
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <button className="btn btn-ghost btn-sm" disabled={disabled || loading} onClick={onLoad}>
          {loading ? `${label}生成中…` : error ? `重试${label}` : loaded ? `刷新${label}` : `获取${label}`}
        </button>
        <span role="status" className="text-xs text-fg-3">
          {loading ? "正在按当前版本批量生成，不会修改审核状态。" : "AI 内容仅供参考，可继续人工审核。"}
        </span>
      </div>
      {error && (
        <div role="alert" className="rounded-lg border border-warn/30 bg-warn/5 p-3 text-xs text-warn">
          <p className="break-words">{label}暂不可用：{error}</p>
          <button className="btn btn-ghost btn-sm mt-2" disabled={disabled} onClick={onExpert}>切换专家模式，人工审核</button>
        </div>
      )}
    </div>
  );
}

export function PreselectionHint({ threshold }: { threshold: number }) {
  return <p className="text-xs leading-relaxed text-fg-3">高置信阈值 {(threshold * 100).toFixed(0)}% · 仅预选，确认后才生效。关键项不会自动勾选。</p>;
}

export function RecommendationCard({ recommendation, currentValue, checked, disabled, onCheck, onAdopt, onModify }: {
  recommendation: FieldRecommendation;
  currentValue: unknown;
  checked: boolean;
  disabled: boolean;
  onCheck: (checked: boolean) => void;
  onAdopt: () => void;
  onModify?: () => void;
}) {
  const display = (value: unknown) => value == null ? "—" : String(value);
  return (
    <div className="space-y-2 rounded-lg border border-info/30 bg-info/5 p-3 text-xs">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <label className="flex items-center gap-2 text-info">
          <input type="checkbox" checked={checked} disabled={disabled} onChange={(event) => onCheck(event.target.checked)}
            aria-label={`选择 AI 推荐：${recommendation.field_path}`} className="h-4 w-4 accent-[var(--color-brand)]" />
          AI 推荐 · {recommendation.action === "modify" ? "修改" : "接受当前值"}
        </label>
        <span className="font-mono text-fg-3">置信度 {(recommendation.confidence * 100).toFixed(0)}%</span>
      </div>
      <p className="break-words font-mono text-fg">
        当前：{display(currentValue)}
        {recommendation.action === "modify" && <> → 建议：{display(recommendation.suggested_value)}</>}
      </p>
      <p className="whitespace-pre-wrap break-words leading-relaxed text-fg-2">理由：{recommendation.reasoning}</p>
      <p className="whitespace-pre-wrap break-words leading-relaxed text-warn">风险：{recommendation.risk}</p>
      <div className="flex flex-wrap gap-2">
        <button className="btn btn-info btn-sm" disabled={disabled} onClick={onAdopt}>采纳此推荐</button>
        {recommendation.action === "modify" && onModify && (
          <button className="btn btn-ghost btn-sm" disabled={disabled} onClick={onModify}
            title="打开字段编辑，预填建议值；保存为新版本并记为人工修改">
            修改…
          </button>
        )}
      </div>
    </div>
  );
}
