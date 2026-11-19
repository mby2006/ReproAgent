"use client";

import { useCallback, useEffect, useState } from "react";
import { apiFetch } from "@/lib/api";
import ReportPanel from "./ReportPanel";
import type {
  ArtifactRecord,
  CodeFileRecord,
  RunAccepted,
  RunDetail,
  RunLogs,
  RunStatus,
  RunSummary,
} from "@/lib/types";

const STATUS_LABEL: Record<RunStatus, string> = {
  queued: "排队中",
  running: "运行中",
  succeeded: "成功",
  failed: "失败",
  timeout: "超时",
  cancelled: "已取消",
};

const STATUS_COLOR: Record<RunStatus, string> = {
  queued: "bg-zinc-700 text-zinc-200",
  running: "bg-sky-700 text-sky-100",
  succeeded: "bg-emerald-700 text-emerald-100",
  failed: "bg-red-800 text-red-100",
  timeout: "bg-amber-700 text-amber-50",
  cancelled: "bg-zinc-600 text-zinc-200",
};

const TERMINAL: RunStatus[] = ["succeeded", "failed", "timeout", "cancelled"];

const ERROR_LABEL: Record<string, string> = {
  SYNTAX_ERROR: "语法错误",
  IMPORT_ERROR: "导入错误",
  SHAPE_MISMATCH: "形状不匹配",
  OUT_OF_MEMORY: "内存溢出",
  TIMEOUT: "超时",
  DATA_DOWNLOAD_ERROR: "数据下载错误",
  METRIC_ERROR: "指标错误",
  UNKNOWN_ERROR: "未知错误",
};

function StatusBadge({ status }: { status: RunStatus }) {
  return (
    <span className={`rounded px-2 py-0.5 text-xs ${STATUS_COLOR[status]}`}>
      {STATUS_LABEL[status]}
    </span>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2">
      <span className="block text-xs text-zinc-500">{label}</span>
      <span className="block break-all text-sm text-zinc-200">{value}</span>
    </div>
  );
}

export default function RunPanel({
  experimentId,
  approved,
  workflowDriven = false,
  workflowStatus = null,
}: {
  experimentId: string;
  approved: boolean;
  workflowDriven?: boolean;
  workflowStatus?: string | null;
}) {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [logs, setLogs] = useState<RunLogs | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactRecord[]>([]);
  const [files, setFiles] = useState<CodeFileRecord[]>([]);
  const [tab, setTab] = useState<"stdout" | "stderr" | "files">("stdout");
  const [openFile, setOpenFile] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const loadRuns = useCallback(async () => {
    const list = await apiFetch<RunSummary[]>(`/v1/experiments/${experimentId}/runs`);
    setRuns(list);
    return list;
  }, [experimentId]);

  const loadDetail = useCallback(async (runId: string) => {
    const [d, l, a, f] = await Promise.all([
      apiFetch<RunDetail>(`/v1/runs/${runId}`),
      apiFetch<RunLogs>(`/v1/runs/${runId}/logs`).catch(() => ({ stdout: "", stderr: "" })),
      apiFetch<ArtifactRecord[]>(`/v1/runs/${runId}/artifacts`).catch(() => []),
      apiFetch<CodeFileRecord[]>(`/v1/runs/${runId}/files`).catch(() => []),
    ]);
    setDetail(d);
    setLogs(l);
    setArtifacts(a);
    setFiles(f);
  }, []);

  useEffect(() => {
    loadRuns()
      .then((list) => {
        if (list.length > 0) setSelectedId((cur) => cur ?? list[0].id);
      })
      .catch((err) => setError(err.message));
  }, [loadRuns]);

  useEffect(() => {
    if (!selectedId) return;
    loadDetail(selectedId).catch((err) => setError(err.message));
  }, [selectedId, loadDetail]);

  useEffect(() => {
    if (!selectedId || !detail || TERMINAL.includes(detail.status)) return;
    const timer = setInterval(() => {
      loadDetail(selectedId).catch(() => {});
      loadRuns().catch(() => {});
    }, 2000);
    return () => clearInterval(timer);
  }, [selectedId, detail, loadDetail, loadRuns]);

  const workflowTerminal = workflowStatus === "completed" || workflowStatus === "failed";

  useEffect(() => {
    if (
      !workflowDriven ||
      workflowTerminal ||
      (detail && TERMINAL.includes(detail.status))
    )
      return;
    const timer = setInterval(() => {
      loadRuns()
        .then((list) => {
          if (list.length > 0) setSelectedId((cur) => cur ?? list[0].id);
        })
        .catch(() => {});
    }, 2000);
    return () => clearInterval(timer);
  }, [workflowDriven, workflowTerminal, detail, loadRuns]);

  async function execute() {
    setStarting(true);
    setError(null);
    try {
      const accepted = await apiFetch<RunAccepted>(`/v1/experiments/${experimentId}/runs`, {
        method: "POST",
      });
      const list = await loadRuns();
      setSelectedId(accepted.run_id);
      if (list.length === 0) setRuns([]);
    } catch (err) {
      setError(err instanceof Error ? err.message : "启动失败");
    } finally {
      setStarting(false);
    }
  }

  async function cancel() {
    if (!selectedId) return;
    setError(null);
    try {
      await apiFetch<RunDetail>(`/v1/runs/${selectedId}/cancel`, { method: "POST" });
      await loadDetail(selectedId);
      await loadRuns();
    } catch (err) {
      setError(err instanceof Error ? err.message : "取消失败");
    }
  }

  const running = detail != null && !TERMINAL.includes(detail.status);
  const metrics = detail?.metrics_json ?? {};
  const audit = detail?.audit_json ?? {};

  return (
    <section className="space-y-3 rounded-xl border border-zinc-800 bg-zinc-950 p-4">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-sm font-semibold uppercase tracking-wide text-zinc-400">
          执行 Run（沙箱容器）
        </h2>
        {!workflowDriven && (
          <button
            onClick={execute}
            disabled={!approved || starting}
            className="rounded bg-emerald-600 px-3 py-1 text-sm hover:bg-emerald-500 disabled:opacity-50"
          >
            {starting ? "启动中…" : "执行实验"}
          </button>
        )}
        {workflowDriven && (
          <span className="text-xs text-zinc-400">由工作流自动执行</span>
        )}
        {running && (
          <button
            onClick={cancel}
            className="rounded bg-red-700 px-3 py-1 text-sm hover:bg-red-600"
          >
            取消运行
          </button>
        )}
        {!workflowDriven && !approved && (
          <span className="text-xs text-amber-400">需先批准计划版本才能执行</span>
        )}
        {runs.length > 0 && (
          <select
            value={selectedId ?? ""}
            onChange={(e) => setSelectedId(e.target.value)}
            className="ml-auto rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-sm"
          >
            {runs.map((r) => (
              <option key={r.id} value={r.id}>
                {r.created_at.slice(0, 19).replace("T", " ")} · {STATUS_LABEL[r.status]} · v
                {r.code_version}
              </option>
            ))}
          </select>
        )}
      </div>

      {error && (
        <p className="rounded-lg border border-red-800 bg-red-950 px-4 py-2 text-sm text-red-300">
          {error}
        </p>
      )}

      {!detail && runs.length === 0 && (
        <p className="text-sm text-zinc-500">
          {workflowDriven
            ? "工作流正在生成代码并启动执行，稍候将显示实时日志与指标。"
            : "尚无运行记录。批准计划后点击「执行实验」。"}
        </p>
      )}

      {detail && (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <StatusBadge status={detail.status} />
            <span className="text-zinc-400">代码 v{detail.code_version}</span>
            <span className="text-zinc-400">计划 v{detail.plan_version}</span>
            <span className="text-zinc-400">修复尝试 {detail.repair_attempt}/3</span>
            {detail.exit_code != null && (
              <span className="text-zinc-400">exit {detail.exit_code}</span>
            )}
            {detail.runtime_json?.execution_time != null && (
              <span className="text-zinc-400">
                耗时 {detail.runtime_json.execution_time.toFixed(1)}s
              </span>
            )}
            {detail.error_category && (
              <span className="rounded bg-red-900 px-2 py-0.5 text-xs text-red-200">
                {ERROR_LABEL[detail.error_category] ?? detail.error_category}
              </span>
            )}
          </div>

          {detail.error && (
            <p className="rounded-lg border border-red-800 bg-red-950 px-4 py-2 text-sm text-red-300">
              {detail.error}
            </p>
          )}

          <div className="grid gap-3 lg:grid-cols-2">
            <div className="space-y-1.5">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
                指标 Metrics
              </h3>
              {Object.keys(metrics).length === 0 ? (
                <p className="text-sm text-zinc-600">（暂无指标输出）</p>
              ) : (
                <div className="grid grid-cols-2 gap-1.5">
                  {Object.entries(metrics).map(([k, v]) => (
                    <Field key={k} label={k} value={String(v)} />
                  ))}
                </div>
              )}
            </div>
            <div className="space-y-1.5">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
                审计 Audit
              </h3>
              <div className="grid grid-cols-2 gap-1.5">
                <Field label="Python" value={audit.python_version ?? "—"} />
                <Field label="随机种子 seed" value={audit.seed ?? "—"} />
                <Field label="镜像 digest" value={audit.image_digest ?? "—"} />
                <Field label="依赖锁 hash" value={audit.dependency_lock_hash ?? "—"} />
                <Field label="数据集指纹" value={audit.dataset_fingerprint ?? "—"} />
                <Field label="代码 hash" value={audit.code_hash ?? detail.code_hash ?? "—"} />
              </div>
            </div>
          </div>

          <div className="space-y-1.5">
            <div className="flex items-center gap-2">
              {(["stdout", "stderr", "files"] as const).map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={`rounded px-2 py-1 text-xs ${
                    tab === t ? "bg-zinc-700 text-zinc-100" : "bg-zinc-900 text-zinc-400"
                  }`}
                >
                  {t === "stdout" ? "标准输出" : t === "stderr" ? "标准错误" : `代码文件 (${files.length})`}
                </button>
              ))}
            </div>
            {tab !== "files" ? (
              <pre className="max-h-72 overflow-auto rounded-lg border border-zinc-800 bg-black px-3 py-2 text-xs text-zinc-300">
                {(tab === "stdout" ? logs?.stdout : logs?.stderr) || "（空）"}
              </pre>
            ) : (
              <div className="space-y-1.5">
                {files.length === 0 && <p className="text-sm text-zinc-600">（无代码文件）</p>}
                {files.map((f) => (
                  <div
                    key={`${f.path}-${f.version}`}
                    className="rounded-lg border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm"
                  >
                    <button
                      onClick={() => setOpenFile(openFile === f.path ? null : f.path)}
                      className="flex w-full items-center justify-between gap-2 text-left"
                    >
                      <span className="font-mono text-zinc-200">{f.path}</span>
                      <span className="shrink-0 text-xs text-zinc-500">
                        v{f.version} · {f.sha256.slice(0, 12)}
                      </span>
                    </button>
                    {f.change_reason && (
                      <span className="block text-xs text-zinc-500">{f.change_reason}</span>
                    )}
                    {f.related_error && (
                      <span className="block text-xs text-red-300">关联错误：{f.related_error}</span>
                    )}
                    {openFile === f.path && f.content && (
                      <pre className="mt-2 max-h-72 overflow-auto rounded border border-zinc-800 bg-black px-3 py-2 text-xs text-zinc-300">
                        {f.content}
                      </pre>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>

          {artifacts.length > 0 && (
            <div className="space-y-1.5">
              <h3 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
                产物 Artifacts（{artifacts.length}）
              </h3>
              <div className="flex flex-wrap gap-1.5">
                {artifacts.map((a) => (
                  <span
                    key={a.id}
                    className="rounded border border-zinc-800 bg-zinc-900 px-2 py-1 text-xs text-zinc-300"
                    title={a.storage_path}
                  >
                    {a.artifact_type}
                  </span>
                ))}
              </div>
            </div>
          )}

          {TERMINAL.includes(detail.status) && <ReportPanel runId={detail.id} />}
        </div>
      )}
    </section>
  );
}