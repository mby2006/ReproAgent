import { supabase } from "./supabase";
import type { CardVersion, ExplainResponse, NodeTrace, PlanVersion, RecommendResponse, OrdinaryReviewMode, RiskTier, WorkflowDetail } from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const {
    data: { session },
  } = await supabase.auth.getSession();
  if (!session) throw new Error("未登录");
  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Bearer ${session.access_token}`);
  const response = await fetch(`${API_URL}${path}`, { ...init, headers });
  if (!response.ok) {
    let detail = `${response.status}`;
    try {
      const body = await response.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep status code */
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
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
  return apiFetch<WorkflowDetail>(`/v1/workflows/${workflowId}`);
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

export function confirmCard(paperId: string, reviewMode?: OrdinaryReviewMode) {
  return apiFetch<CardVersion>(`/v1/papers/${paperId}/card/confirm`, {
    method: "POST",
    ...(reviewMode ? {
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ review_mode: reviewMode }),
    } : {}),
  });
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