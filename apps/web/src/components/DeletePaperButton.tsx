"use client";

import { useId, useRef, useState } from "react";
import { deletePaper } from "@/lib/api";
import type { PaperSummary } from "@/lib/types";
import { IconTrash } from "./icons";

export default function DeletePaperButton({
  paper,
  disabled = false,
  onDeleted,
}: {
  paper: PaperSummary;
  disabled?: boolean;
  onDeleted: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const descriptionId = useId();
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function remove() {
    if (deleting) return;
    setDeleting(true);
    setError(null);
    try {
      await deletePaper(paper.id);
      dialog.current?.close();
      onDeleted();
    } catch (err) {
      setError(err instanceof Error ? err.message : "删除失败，请重试");
    } finally {
      setDeleting(false);
    }
  }

  return (
    <>
      <button
        type="button"
        disabled={disabled || deleting}
        onClick={() => {
          setError(null);
          dialog.current?.showModal();
        }}
        className="btn btn-ghost btn-sm text-bad!"
        aria-label={`删除论文：${paper.title}`}
        title={disabled ? "任务进行中，暂不可删除" : "删除论文"}
      >
        <IconTrash className="h-3.5 w-3.5" /> 删除
      </button>
      <dialog
        ref={dialog}
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        onCancel={(event) => {
          if (deleting) event.preventDefault();
        }}
        className="card fixed inset-0 m-auto w-[calc(100%-2rem)] max-w-md p-6 text-fg backdrop:bg-black/70"
      >
        <h2 id={titleId} className="display text-lg font-semibold">删除这篇论文？</h2>
        <div id={descriptionId} className="mt-3 space-y-2 text-sm text-fg-2">
          <p className="break-words font-medium text-fg">{paper.title}</p>
          <p>将永久删除 PDF、解析结果及关联的实验、运行记录和报告。此操作不可撤销。</p>
        </div>
        {error && (
          <p role="alert" className="mt-4 rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-sm text-bad">
            {error}
          </p>
        )}
        <div className="mt-6 flex justify-end gap-2">
          <button
            type="button"
            autoFocus
            disabled={deleting}
            onClick={() => dialog.current?.close()}
            className="btn btn-ghost btn-sm"
          >
            取消
          </button>
          <button
            type="button"
            disabled={deleting}
            onClick={remove}
            className="btn btn-sm border-bad/40! bg-bad/15! text-bad! hover:bg-bad/25!"
          >
            <IconTrash className="h-3.5 w-3.5" />
            {deleting ? "删除中…" : "确认删除"}
          </button>
        </div>
      </dialog>
    </>
  );
}
