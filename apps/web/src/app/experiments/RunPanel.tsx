"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { IconPlay, IconStop } from "@/components/icons";
import { apiFetch, apiGet, cancelWorkflow } from "@/lib/api";
import { startVisiblePolling } from "@/lib/polling";
import { PrivatePredictions } from "@/components/PredictionReader";
import ReportPanel from "./ReportPanel";
import EvidenceIndex from "./EvidenceIndex";
import type {
  ArtifactRecord,
  CodeFileRecord,
  EvidenceIndex as EvidenceIndexData,
  RunAccepted,
  RunDetail,
  RunLogs,
  RunStatus,
  RunSummary,
  WorkflowStatus,
} from "@/lib/types";
import { ERROR_LABEL, isWorkflowTerminal, RUN_META, RUN_TERMINAL, TONE_BADGE, fmtDate } from "@/lib/ui";

export function findNewRun<T extends { id: string }>(runs: T[], knownIds: ReadonlySet<string> | null) {
  if (!knownIds) return undefined;
  return runs.find((run) => !knownIds.has(run.id));
}

function StatusBadge({ status }: { status: RunStatus }) {
  const meta = RUN_META[status];
  return (
    <span className={`chip ${TONE_BADGE[meta.tone]}`}>
      <span
        className={`h-1.5 w-1.5 rounded-full ${
          meta.tone === "info" ? "pulse-dot bg-info" : meta.tone === "ok" ? "bg-ok" : meta.tone === "bad" ? "bg-bad" : meta.tone === "warn" ? "bg-warn" : "bg-fg-3"
        }`}
      />
      {meta.label}
    </span>
  );
}

function AuditField({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-line-soft bg-ink-950/60 px-3 py-2">
      <span className="label block text-[11px]!">{label}</span>
      <span className="block truncate font-mono text-[12px] text-fg-2" title={String(value)}>
        {value}
      </span>
    </div>
  );
}

function MetricTile({ name, value, target, unitKey }: { unitKey: string; name: string; value: unknown; target: boolean }) {
  const numeric = typeof value === "number";
  const isRatio =
    numeric && (unitKey === "accuracy" || unitKey === "error_rate") && value >= 0 && value <= 1;
  const display = numeric
    ? isRatio
      ? `${(value * 100).toFixed(2)}%`
      : Number.isInteger(value)
        ? String(value)
        : value.toFixed(4)
    : String(value);
  return (
    <div
      className={`rounded-xl border px-3 py-2.5 transition ${
        target
          ? "border-brand/45 bg-brand/8 shadow-[0_0_24px_-14px_var(--color-brand)]"
          : "border-line-soft bg-ink-950/60"
      }`}
    >
      <span className="label flex items-center gap-1.5 text-[11px]!">
        {name}
        {target && <span className="chip badge-brand px-1! py-0! text-[8.5px]!">target</span>}
      </span>
      <span
        className={`display mt-1 block truncate text-xl font-bold ${target ? "text-brand" : "text-fg"}`}
        title={String(value)}
      >
        {display}
      </span>
    </div>
  );
}

function LossBars({ train, test }: { train: number; test: number }) {
  const max = Math.max(train, test, 1e-9);
  const rows = [
    { label: "train_loss", value: train, color: "var(--color-info)" },
    { label: "test_loss", value: test, color: "var(--color-scale)" },
  ];
  return (
    <div className="card space-y-2 p-3">
      <p className="label text-[11px]!">Loss Overview</p>
      {rows.map((r) => (
        <div key={r.label} className="space-y-1">
          <div className="flex items-center justify-between font-mono text-[10px] text-fg-3">
            <span>{r.label}</span>
            <span className="text-fg-2">{r.value.toFixed(4)}</span>
          </div>
          <div className="meter h-2!">
            <span style={{ width: `${(r.value / max) * 100}%`, background: r.color }} />
          </div>
        </div>
      ))}
    </div>
  );
}

export default function RunPanel({
  experimentId,
  approved,
  workflowDriven = false,
  workflowStatus = null,
  workflowId = null,
}: {
  experimentId: string;
  approved: boolean;
  workflowDriven?: boolean;
  workflowStatus?: WorkflowStatus | null;
  workflowId?: string | null;
}) {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [logs, setLogs] = useState<RunLogs | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactRecord[]>([]);
  const [files, setFiles] = useState<CodeFileRecord[]>([]);
  const [evidenceIndex, setEvidenceIndex] = useState<EvidenceIndexData | null>(null);
  const [tab, setTab] = useState<"stdout" | "stderr" | "files">("stdout");
  const [openFile, setOpenFile] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [filesLoadedFor, setFilesLoadedFor] = useState<string | null>(null);

  const requestVersion = useRef(0);
  const filesVersion = useRef(0);
  const artifactsVersion = useRef(0);
  const listVersion = useRef(0);
  const evidenceVersion = useRef(0);
  const detailInFlight = useRef<string | null>(null);
  const listInFlight = useRef<string | null>(null);
  const knownRunIds = useRef<Set<string> | null>(null);
  const loadedExperiment = useRef<string | null>(null);
  const artifactsLoadedFor = useRef<string | null>(null);

  const loadRuns = useCallback(async () => {
    if (listInFlight.current === experimentId) return null;
    listInFlight.current = experimentId;
    const version = ++listVersion.current;
    try {
      const list = await apiGet<RunSummary[]>(`/v1/experiments/${experimentId}/runs`);
      if (list && version === listVersion.current) {
        if (loadedExperiment.current !== experimentId) {
          loadedExperiment.current = experimentId;
          knownRunIds.current = null;
        }
        if (workflowDriven && knownRunIds.current) {
          const appeared = findNewRun(list, knownRunIds.current);
          if (appeared) setSelectedId(appeared.id);
        }
        knownRunIds.current = new Set(list.map((run) => run.id));
        setRuns(list);
      }
      return list;
    } catch (err) {
      if (version === listVersion.current) {
        setError(err instanceof Error ? err.message : "无法读取运行记录");
      }
      return null;
    } finally {
      if (listInFlight.current === experimentId) listInFlight.current = null;
    }
  }, [experimentId, workflowDriven]);

  const loadDetail = useCallback(async (runId: string) => {
    if (detailInFlight.current === runId) return;
    const version = ++requestVersion.current;
    detailInFlight.current = runId;
    try {
      const [d, l] = await Promise.all([
        apiGet<RunDetail>(`/v1/runs/${runId}`),
        apiGet<RunLogs>(`/v1/runs/${runId}/logs`).catch(() => ({ stdout: "", stderr: "" })),
      ]);
      if (d && version === requestVersion.current) {
        setDetail(d);
        setLogs(l ?? { stdout: "", stderr: "" });
        setError(null);
      }
    } catch (err) {
      if (version === requestVersion.current) {
        setError(err instanceof Error ? err.message : "无法读取运行详情");
      }
    } finally {
      if (detailInFlight.current === runId) detailInFlight.current = null;
    }
  }, []);

  useEffect(() => {
    setSelectedId(null);
    setRuns([]);
  }, [experimentId]);

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  useEffect(() => {
    requestVersion.current++;
    filesVersion.current++;
    artifactsVersion.current++;
    setDetail(null);
    setLogs(null);
    setArtifacts([]);
    setFiles([]);
    setEvidenceIndex(null);
    setFilesLoadedFor(null);
    setOpenFile(null);
    artifactsLoadedFor.current = null;
    if (selectedId) void loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  useEffect(() => {
    if (!selectedId) return;
    const version = ++evidenceVersion.current;
    void apiGet<EvidenceIndexData>(`/v1/runs/${selectedId}/evidence-index`)
      .then((index) => {
        if (index && version === evidenceVersion.current) setEvidenceIndex(index);
      })
      .catch((err) => {
        if (version === evidenceVersion.current) setError(err instanceof Error ? err.message : "无法读取证据索引");
      });
  }, [selectedId, detail?.status]);

  const workflowTerminal = workflowStatus != null && isWorkflowTerminal(workflowStatus);
  const hasDetail = detail != null;
  const detailTerminal = hasDetail && RUN_TERMINAL.includes(detail.status);
  const detailActive = selectedId != null && (detail == null || !RUN_TERMINAL.includes(detail.status));
  const listActive = detailActive || (workflowDriven && !workflowTerminal);

  useEffect(() => {
    if (!listActive && !detailActive) return;
    return startVisiblePolling(() => {
      if (listActive) void loadRuns();
      if (detailActive && selectedId) void loadDetail(selectedId);
    }, 2000);
  }, [listActive, detailActive, selectedId, loadDetail, loadRuns]);

  useEffect(() => {
    if (tab !== "files" || !selectedId || !hasDetail || filesLoadedFor === selectedId) return;
    const runId = selectedId;
    const version = ++filesVersion.current;
    void apiGet<CodeFileRecord[]>(`/v1/runs/${runId}/files`)
      .then((list) => {
        if (!list || version !== filesVersion.current) return;
        setFilesLoadedFor(runId);
        setFiles(list);
      })
      .catch((err) => {
        if (version === filesVersion.current) {
          setError(err instanceof Error ? err.message : "无法读取代码文件");
        }
      });
  }, [tab, selectedId, detail?.id, hasDetail, filesLoadedFor]);

  useEffect(() => {
    if (!selectedId || !detailTerminal || artifactsLoadedFor.current === selectedId) return;
    const runId = selectedId;
    const version = ++artifactsVersion.current;
    void apiGet<ArtifactRecord[]>(`/v1/runs/${runId}/artifacts`)
      .then((list) => {
        if (!list || version !== artifactsVersion.current) return;
        artifactsLoadedFor.current = runId;
        setArtifacts(list);
      })
      .catch((err) => {
        if (version === artifactsVersion.current) {
          setError(err instanceof Error ? err.message : "无法读取训练产物");
        }
      });
  }, [selectedId, detail?.id, detailTerminal]);

  async function execute() {
    setStarting(true);
    setError(null);
    try {
      const accepted = await apiFetch<RunAccepted>(`/v1/experiments/${experimentId}/runs`, {
        method: "POST",
      });
      await loadRuns();
      setSelectedId(accepted.run_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "启动失败");
    } finally {
      setStarting(false);
    }
  }

  async function cancel() {
    if (!selectedId || cancelling) return;
    setCancelling(true);
    setError(null);
    try {
      await apiFetch<RunDetail>(`/v1/runs/${selectedId}/cancel`, { method: "POST" });
      await loadDetail(selectedId);
      await loadRuns();
    } catch (err) {
      setError(err instanceof Error ? err.message : "取消失败");
    } finally {
      setCancelling(false);
    }
  }

  // 取消入口在 Run 之外同样可用：workflow 尚处于非终态的任意阶段都能停止，
  // 取消是独立终态，不会被前端折算成失败或成功。
  async function stopWorkflow() {
    if (!workflowId || cancelling) return;
    setCancelling(true);
    setError(null);
    try {
      await cancelWorkflow(workflowId);
      await loadRuns();
      if (selectedId) await loadDetail(selectedId);
    } catch (err) {
      setError(err instanceof Error ? err.message : "停止工作流失败");
    } finally {
      setCancelling(false);
    }
  }

  const running = detail != null && !RUN_TERMINAL.includes(detail.status);
  const hasActiveRun = running || runs.some((r) => !RUN_TERMINAL.includes(r.status));
  const canStopWorkflow = workflowId != null && !workflowTerminal && !hasActiveRun;
  const metrics = detail?.metrics_json ?? {};
  const audit = detail?.audit_json ?? {};
  const targetMetric = typeof metrics.target_metric === "string" ? metrics.target_metric : null;
  const legacyTarget = metrics.schema_version !== "1.1" && targetMetric != null && !["accuracy", "test accuracy"].includes(targetMetric.toLowerCase());
  const trainLoss = typeof metrics.train_loss === "number" ? metrics.train_loss : null;
  const testLoss = typeof metrics.test_loss === "number" ? metrics.test_loss : null;

  function handleTabKey(event: React.KeyboardEvent<HTMLDivElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    const tabs = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]'));
    const current = tabs.findIndex((item) => item === document.activeElement);
    if (current < 0) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1
      : (current + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    tabs[next].focus();
    setTab(tabs[next].value as typeof tab);
  }

  return (
    <section className="card rise space-y-4 p-4" aria-busy={starting || cancelling}>
      <div className="flex flex-wrap items-center gap-3">
        <div>
          <p className="label mb-0.5">Execution · 沙箱容器</p>
          <h2 className="display text-lg font-bold">训练执行</h2>
        </div>
        {!workflowDriven && (
          <button onClick={execute} disabled={!approved || starting} aria-busy={starting} className="btn btn-primary btn-sm">
            <IconPlay className="h-3.5 w-3.5" />
            {starting ? "启动中…" : "执行实验"}
          </button>
        )}
        {workflowDriven && <span className="chip">由工作流自动执行</span>}
        {running && (
          <button onClick={cancel} disabled={cancelling} aria-busy={cancelling} className="btn btn-danger btn-sm">
            <IconStop className="h-3.5 w-3.5" /> {cancelling ? "取消中…" : "取消运行"}
          </button>
        )}
        {canStopWorkflow && (
          <button onClick={stopWorkflow} disabled={cancelling} aria-busy={cancelling} className="btn btn-danger btn-sm">
            <IconStop className="h-3.5 w-3.5" /> {cancelling ? "停止中…" : "停止工作流"}
          </button>
        )}
        {!workflowDriven && !approved && (
          <span className="text-xs text-warn">需先批准计划版本才能执行</span>
        )}
        {runs.length > 0 && (
          <select
            value={selectedId ?? ""}
            onChange={(e) => setSelectedId(e.target.value)}
            className="select ml-auto w-64!"
            aria-label="选择运行"
          >
            {runs.map((r) => (
              <option key={r.id} value={r.id}>
                {fmtDate(r.created_at)} · {RUN_META[r.status].label} · v{r.code_version}
              </option>
            ))}
          </select>
        )}
      </div>

      {error && (
        <p role="alert" className="rounded-lg border border-bad/40 bg-bad/10 px-4 py-2 text-sm text-bad">{error}</p>
      )}

      {!detail && runs.length > 0 && (
        <p role="status" aria-live="polite" className="rounded-xl border border-dashed border-line p-6 text-center text-sm text-fg-3">
          请选择一条运行记录查看详情。
        </p>
      )}

      {!detail && runs.length === 0 && (
        <p className="rounded-xl border border-dashed border-line p-6 text-center text-sm text-fg-3">
          {workflowDriven
            ? "工作流正在生成代码并启动执行，稍候将显示实时日志与指标。"
            : "尚无运行记录。批准计划后点击「执行实验」。"}
        </p>
      )}

      {detail && detail.id === selectedId && (
        <div className="space-y-4">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status={detail.status} />
            <span className="chip">code v{detail.code_version}</span>
            <span className="chip">plan v{detail.plan_version}</span>
            <span className="chip">repair {detail.repair_attempt}/3</span>
            {detail.exit_code != null && <span className="chip">exit {detail.exit_code}</span>}
            {detail.runtime_json?.execution_time != null && (
              <span className="chip">{detail.runtime_json.execution_time.toFixed(1)}s</span>
            )}
            {detail.error_category && (
              <span className="chip badge-bad">
                {ERROR_LABEL[detail.error_category] ?? detail.error_category}
              </span>
            )}
          </div>

          {detail.error && (
            <p className="rounded-lg border border-bad/40 bg-bad/10 px-4 py-2 text-sm text-bad">
              {detail.error}
            </p>
          )}

          <div className="grid gap-4 lg:grid-cols-[1.2fr_1fr]">
            <div className="space-y-2">
              <h3 className="label">训练指标</h3>
              {Object.keys(metrics).length === 0 ? (
                <p className="text-sm text-fg-3">（暂无指标输出）</p>
              ) : (
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                  {legacyTarget && <p className="text-sm text-warn">旧版目标值的单位无法核实，请参阅独立记录的准确率。</p>}
                  {Object.entries(metrics).filter(([k]) => !(legacyTarget && k === "metric_value")).map(([k, v]) => (
                    <MetricTile key={k} unitKey={k === "metric_value" ? targetMetric ?? "" : k} name={k} value={v} target={k === targetMetric || k === "metric_value"} />
                  ))}
                </div>
              )}
              {trainLoss != null && testLoss != null && (
                <LossBars train={trainLoss} test={testLoss} />
              )}
            </div>
            <details className="space-y-2">
              <summary className="label cursor-pointer">
                环境审计（技术细节）
                <span className="ml-2 font-normal text-fg-3">核对这次跑的环境和数据集是否和记录一致</span>
              </summary>
              <div className="mt-2 grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                <AuditField label="Python 版本" value={audit.python_version ?? "—"} />
                <AuditField label="随机种子" value={audit.seed ?? "—"} />
                <AuditField label="执行镜像指纹" value={audit.image_digest ?? "—"} />
                <AuditField label="依赖锁指纹" value={audit.dependency_lock_hash ?? "—"} />
                <AuditField label="数据集指纹" value={audit.dataset_fingerprint ?? "—"} />
                <AuditField label="代码指纹" value={audit.code_hash ?? detail.code_hash ?? "—"} />
              </div>
            </details>
          </div>

          <div className="terminal">
            <div className="terminal-bar">
              <span className="terminal-dot bg-bad/70" />
              <span className="terminal-dot bg-warn/70" />
              <span className="terminal-dot bg-ok/70" />
              <div role="tablist" aria-label="运行输出" onKeyDown={handleTabKey} className="ml-3 flex gap-1">
                {(["stdout", "stderr", "files"] as const).map((t) => (
                  <button
                    key={t}
                    type="button"
                    role="tab"
                    id={`run-tab-${t}`}
                    aria-controls="run-output-panel"
                    aria-selected={tab === t}
                    tabIndex={tab === t ? 0 : -1}
                    value={t}
                    onClick={() => setTab(t)}
                    className={`tab ${tab === t ? "tab-active" : ""}`}
                  >
                    {t === "stdout" ? "stdout" : t === "stderr" ? "stderr" : `files (${files.length})`}
                  </button>
                ))}
              </div>
              {running && (
                <span role="status" aria-live="polite" className="ml-auto flex items-center gap-1.5 font-mono text-[10px] text-info">
                  <span className="pulse-dot h-1.5 w-1.5 rounded-full bg-info" /> live
                </span>
              )}
            </div>
            <div
              role="tabpanel"
              id="run-output-panel"
              aria-labelledby={`run-tab-${tab}`}
              aria-busy={tab === "files" && filesLoadedFor !== selectedId}
            >
            {tab !== "files" ? (
              <pre className="max-h-80 overflow-auto px-3 py-2 font-mono text-[12px] leading-relaxed text-fg-2">
                {(tab === "stdout" ? logs?.stdout : logs?.stderr) || "（空）"}
                {running && <span className="animate-blink text-brand">▍</span>}
              </pre>
            ) : (
              <div className="space-y-1.5 p-3">
                {files.length === 0 && <p role="status" className="text-sm text-fg-3">
                  {filesLoadedFor === selectedId ? "（无代码文件）" : "正在读取代码文件…"}
                </p>}
                {files.map((f) => (
                  <div id={`run-file-${f.path}`} key={`${f.path}-${f.version}`} className="rounded-lg border border-line-soft bg-ink-900/80 px-3 py-2 text-sm">
                    <button
                      onClick={() => setOpenFile(openFile === f.path ? null : f.path)}
                      aria-expanded={openFile === f.path}
                      aria-controls={`run-file-content-${f.path}`}
                      className="flex w-full items-center justify-between gap-2 text-left"
                    >
                      <span className="font-mono text-[12px] text-fg">{f.path}</span>
                      <span className="shrink-0 font-mono text-[10px] text-fg-3">
                        v{f.version} · {f.sha256.slice(0, 12)}
                      </span>
                    </button>
                    {f.change_reason && (
                      <span className="block text-xs text-fg-3">{f.change_reason}</span>
                    )}
                    {f.related_error && (
                      <span className="block text-xs text-bad">关联错误：{f.related_error}</span>
                    )}
                    {openFile === f.path && f.content && (
                      <pre id={`run-file-content-${f.path}`} className="mt-2 max-h-72 overflow-auto rounded-lg bg-ink-950 px-3 py-2 font-mono text-[12px] leading-relaxed text-fg-2">
                        {f.content}
                      </pre>
                    )}
                  </div>
                ))}
              </div>
            )}
            </div>
          </div>

          {artifacts.length > 0 && (
            <div className="space-y-1.5">
              <h3 className="label">训练产物（{artifacts.length}）</h3>
              <div className="flex flex-wrap gap-1.5">
                {artifacts.map((a) => (
                  <span key={a.id} className="chip" title={a.storage_path}>
                    {a.artifact_type}
                  </span>
                ))}
              </div>
            </div>
          )}

          <EvidenceIndex index={evidenceIndex} />

          {detail.status === "succeeded" && <section className="space-y-3">
            <h2 className="display text-xl font-semibold">这次实验认对了多少张？</h2>
            {typeof metrics.correct_count === "number" && typeof metrics.evaluated_count === "number" &&
              <p>在 {metrics.evaluated_count} 张测试图片中，模型认对了 {metrics.correct_count} 张。下方展示按测试顺序保存的样本。</p>}
            <PrivatePredictions key={detail.id} runId={detail.id}/>
          </section>}
          {RUN_TERMINAL.includes(detail.status) && <ReportPanel key={detail.id} runId={detail.id} onFile={path => {
            setTab("files"); setOpenFile(path);
            requestAnimationFrame(() => document.getElementById(`run-file-${path}`)?.scrollIntoView({block:"center"}));
          }} />}
        </div>
      )}
    </section>
  );
}