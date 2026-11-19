import { Fragment } from "react";
import type { WorkflowStatus } from "@/lib/types";
import { TONE_BADGE, TONE_DOT, TONE_TEXT, WF_META } from "@/lib/ui";
import { IconCheck, IconRefresh } from "./icons";

const ORDER: WorkflowStatus[] = [
  "uploaded",
  "parsing",
  "awaiting_spec_review",
  "planning",
  "awaiting_plan_approval",
  "generating_code",
  "validating_code",
  "executing",
  "repairing",
  "evaluating",
  "completed",
  "failed",
];

export default function WorkflowStepper({
  status,
  currentNode,
  error,
  onRetry,
  retrying,
}: {
  status: WorkflowStatus;
  currentNode?: string | null;
  error?: string | null;
  onRetry?: () => void;
  retrying?: boolean;
}) {
  const activeIdx = ORDER.indexOf(status);
  return (
    <div className="card rise space-y-2.5 p-3">
      <div className="flex items-center overflow-x-auto pb-1">
        {ORDER.map((key, i) => {
          const meta = WF_META[key];
          const active = i === activeIdx;
          const done = i < activeIdx;
          return (
            <Fragment key={key}>
              {i > 0 && (
                <span
                  className={`h-px w-3 shrink-0 sm:w-5 ${done || active ? "bg-brand/45" : "bg-line"}`}
                />
              )}
              <span
                className={`flex shrink-0 items-center gap-1.5 rounded-full border px-2.5 py-1 font-mono text-[10.5px] transition ${
                  active
                    ? `${TONE_BADGE[meta.tone]} font-semibold`
                    : done
                      ? "border-line-soft bg-ink-900 text-fg-2"
                      : "border-transparent text-fg-3"
                }`}
                title={key}
              >
                {done ? (
                  <IconCheck className="h-3 w-3 text-ok" />
                ) : active ? (
                  <span className={`pulse-dot h-1.5 w-1.5 rounded-full ${TONE_DOT[meta.tone]}`} />
                ) : (
                  <span className="h-1.5 w-1.5 rounded-full border border-line" />
                )}
                {meta.label}
              </span>
            </Fragment>
          );
        })}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <p className="font-mono text-[10.5px] text-fg-3">
          current_node: <span className="text-fg-2">{currentNode ?? "—"}</span>
        </p>
        {status === "failed" && onRetry && (
          <button onClick={onRetry} disabled={retrying} className="btn btn-danger btn-sm ml-auto">
            <IconRefresh className="h-3.5 w-3.5" />
            {retrying ? "重试中…" : "从断点重试"}
          </button>
        )}
      </div>
      {status === "failed" && error && (
        <p className={`rounded-lg border border-bad/40 bg-bad/10 px-3 py-1.5 text-xs ${TONE_TEXT.bad}`}>
          {error}
        </p>
      )}
    </div>
  );
}