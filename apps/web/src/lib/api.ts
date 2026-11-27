import { supabase } from "./supabase";
import { readOnce } from "./polling";
import type { CardVersion, ExplainResponse, NodeTrace, PlanVersion, RecommendResponse, OrdinaryReviewMode, RiskTier, WorkflowCancelResponse, WorkflowDetail } from "./types";
import type { CandidateResponse, ComparisonDetail, ComparisonPlanDetail } from "./comparison";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/**
 * 把 API 返回的相对路径补成可直接导航的绝对地址。
 * 用于 <iframe src> 和新标签页链接：这两种请求加不了 Authorization 头，
 * 只能靠服务端签在 URL 里的 capability 签名自证身份。
 */
export const apiHref = (path: string) => `${API_URL}${path}`;

/**
 * API 错误的结构化表示：HTTP 状态码与业务错误码分开保存。
 * 控制流只读 `code`（稳定英文标识符），`message` 仅用于展示，
 * 中文文案变化不影响分支逻辑。
 */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  constructor(message: string, status: number, code: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

/** 把非 2xx 响应解析成 ApiError：{detail:{code,message}} 是稳定契约，
 * 字符串 detail 与无法解析的 body 一律映射为 UNKNOWN_ERROR。 */
async function apiErrorFrom(response: Response): Promise<ApiError> {
  let message = response.statusText || `${response.status}`;
  let code = "UNKNOWN_ERROR";
  try {
    const body: unknown = await response.json();
    const detail = (body as { detail?: unknown } | null)?.detail;
    if (typeof detail === "string") {
      message = detail;
    } else if (detail && typeof detail === "object") {
      const { code: c, message: m } = detail as { code?: unknown; message?: unknown };
      if (typeof c === "string") code = c;
      message = typeof m === "string" ? m : message;
    }
  } catch {
    /* keep status text -- body was absent or not JSON */
  }
  return new ApiError(message, response.status, code);
}

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (!session) throw new Error("未登录");
  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Bearer ${session.access_token}`);
  const response = await fetch(`${API_URL}${path}`, { ...init, headers });
  if (!response.ok) throw await apiErrorFrom(response);
  return (await response.json()) as T;
}

export async function apiDownload(path: string, fallbackName = "artifact") {
  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (!session) throw new Error("未登录");
  const response = await fetch(`${API_URL}${path}`, {
    headers: { Authorization: `Bearer ${session.access_token}` },
  });
  if (!response.ok) throw await apiErrorFrom(response);
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = fallbackName;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 0);
}

export async function apiGet<T>(path: string, init?: RequestInit): Promise<T | null> {
  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (!session) throw new Error("未登录");
  return readOnce(`reproagent-read:${API_URL}${path}:${session.user.id}`, () => apiFetch<T>(path, init));
}

export function uploadPaper(file: File | null, title: string, url?: string) {
  const form = new FormData();
  if (file) form.append("file", file);
  if (url) form.append("url", url);
  form.append("title", title);
  return apiFetch<{ paper_id: string; workflow_id: string; thread_id: string }>("/v1/papers", {
    method: "POST",
    body: form,
  });
}

export function deletePaper(paperId: string) {
  return apiFetch<{ paper_id: string; status: "deleted" }>(`/v1/papers/${paperId}`, {
    method: "DELETE",
  });
}

export function getWorkflow(workflowId: string) {
  return apiGet<WorkflowDetail>(`/v1/workflows/${workflowId}`);
}

export function getPaperWorkflow(paperId: string) {
  return apiFetch<WorkflowDetail>(`/v1/papers/${paperId}/workflow`);
}

export function getWorkflowTraces(workflowId: string) {
  return apiFetch<NodeTrace[]>(`/v1/workflows/${workflowId}/traces`);
}

export function resumeWorkflow(workflowId: string, stage: "spec" | "plan") {
  return apiFetch<{ workflow_id: string; stage: string; status: string }>(
    `/v1/workflows/${workflowId}/resume`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage }),
    },
  );
}

export function retryWorkflow(workflowId: string) {
  return apiFetch<{ workflow_id: string; status: string }>(
    `/v1/workflows/${workflowId}/retry`,
    { method: "POST" },
  );
}

export function cancelWorkflow(workflowId: string) {
  return apiFetch<WorkflowCancelResponse>(
    `/v1/workflows/${workflowId}/cancel`,
    { method: "POST" },
  );
}

export function confirmCard(paperId: string, reviewMode?: OrdinaryReviewMode) {
  return apiFetch<CardVersion>(`/v1/papers/${paperId}/card/confirm`, {
    method: "POST",
    ...(reviewMode ? {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ review_mode: reviewMode }),
    } : {}),
  });
}

export async function fetchCardMarkdown(paperId: string, version?: number): Promise<string> {
  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (!session) throw new Error("未登录");
  const query = version != null ? `?version=${version}` : "";
  const response = await fetch(`${API_URL}/v1/papers/${paperId}/card.md${query}`, {
    headers: { Authorization: `Bearer ${session.access_token}` },
  });
  if (!response.ok) throw await apiErrorFrom(response);
  return response.text();
}

export function delegateCard(paperId: string, baseVersion: number) {
  return apiFetch<CardVersion>(`/v1/papers/${paperId}/card/delegate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ base_version: baseVersion, consent: true }),
  });
}

export function delegatePlan(experimentId: string, version: number) {
  return apiFetch<PlanVersion>(`/v1/experiments/${experimentId}/plans/${version}/delegate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ base_version: version, consent: true }),
  });
}

export function explainCard(paperId: string, baseVersion: number, signal?: AbortSignal) {
  return apiFetch<ExplainResponse>(`/v1/papers/${paperId}/card/explain`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ base_version: baseVersion, field_paths: [] }),
    signal,
  });
}

export function confirmCardBatch(
  paperId: string, baseVersion: number, fieldPaths: string[], contextHash: string, reviewMode: OrdinaryReviewMode,
) {
  return apiFetch<CardVersion>(`/v1/papers/${paperId}/card/confirm-batch`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      base_version: baseVersion, field_paths: fieldPaths, context_hash: contextHash, review_mode: reviewMode,
    }),
  });
}

export function explainPlan(experimentId: string, version: number, signal?: AbortSignal) {
  return apiFetch<ExplainResponse>(`/v1/experiments/${experimentId}/plans/${version}/explain`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ field_paths: [] }),
    signal,
  });
}

export function recommendPlan(experimentId: string, version: number, signal?: AbortSignal) {
  return apiFetch<RecommendResponse>(`/v1/experiments/${experimentId}/plans/${version}/recommend`, {
    method: "POST", signal,
  });
}

export function adoptPlanRecommendations(
  experimentId: string, version: number, recommendationIds: string[], contextHash: string, reviewMode: OrdinaryReviewMode,
) {
  return apiFetch<PlanVersion>(`/v1/experiments/${experimentId}/plans/${version}/adopt`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      recommendation_ids: recommendationIds, context_hash: contextHash, review_mode: reviewMode,
    }),
  });
}

export function overridePlanTier(experimentId: string, version: number, fieldPath: string, riskTier: RiskTier) {
  return apiFetch<PlanVersion>(`/v1/experiments/${experimentId}/plans/${version}/tier`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ field_path: fieldPath, risk_tier: riskTier }),
  });
}
// --- paired optimizer comparison -----------------------------------------

export function getComparisonCandidates(paperId: string, question?: string) {
  return apiFetch<CandidateResponse>(`/v1/papers/${paperId}/comparison-candidates`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question: question ?? null }),
  });
}

export function createComparisonPlan(paperId: string, body: CreateComparisonPlanBody) {
  return apiFetch<ComparisonPlanDetail>(`/v1/papers/${paperId}/comparison-plans`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function getComparisonPlan(planId: string) {
  return apiFetch<ComparisonPlanDetail>(`/v1/comparison-plans/${planId}`);
}

export function reviseComparisonPlan(planId: string, body: ReviseComparisonPlanBody) {
  return apiFetch<ComparisonPlanDetail>(`/v1/comparison-plans/${planId}/versions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function approveComparisonPlan(planId: string, version: number) {
  return apiFetch<ComparisonPlanDetail>(
    `/v1/comparison-plans/${planId}/versions/${version}/approve`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_version: version }),
    },
  );
}

export function startComparison(planId: string, version: number, idempotencyKey: string) {
  return apiFetch<{ comparison_id: string; status: string }>(
    `/v1/comparison-plans/${planId}/versions/${version}/start`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: JSON.stringify({ base_version: version }),
    },
  );
}

export function getComparison(comparisonId: string) {
  return apiGet<ComparisonDetail>(`/v1/comparisons/${comparisonId}`);
}

export function getComparisonReport(comparisonId: string) {
  return apiGet<{ comparison_id: string; status: string; markdown: string; created_at: string }>(
    `/v1/comparisons/${comparisonId}/report`,
  );
}

export function rerunComparison(
  comparisonId: string,
  body: { use_current_environment: boolean; reason: string },
) {
  return apiFetch<{ comparison_id: string; status: string; warnings?: string[] }>(
    `/v1/comparisons/${comparisonId}/rerun`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    },
  );
}

async function authedFetch(path: string): Promise<Response> {
  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (!session) throw new Error("未登录");
  const response = await fetch(`${API_URL}${path}`, {
    headers: { Authorization: `Bearer ${session.access_token}` },
  });
  if (!response.ok) throw await apiErrorFrom(response);
  return response;
}

export async function downloadComparisonExport(comparisonId: string): Promise<Blob> {
  const response = await authedFetch(`/v1/comparisons/${comparisonId}/export`);
  return response.blob();
}

export interface CreateComparisonPlanBody {
  question: string;
  groups: Array<{
    key: "a" | "b";
    label: string;
    optimizer: string;
    learning_rate: number | null;
    references: Array<Record<string, unknown>>;
  }>;
  name?: string | null;
  seeds?: number[] | null;
  train_subset_size?: number | null;
  test_subset_size?: number | null;
  epochs?: number | null;
  batch_size?: number | null;
}

export interface ReviseComparisonPlanBody {
  base_version: number;
  change_reason: string;
  question?: string | null;
  adopt_pending_ids?: string[];
  provided_pending?: Record<string, string>;
  train_subset_size?: number | null;
  test_subset_size?: number | null;
  epochs?: number | null;
  batch_size?: number | null;
}
