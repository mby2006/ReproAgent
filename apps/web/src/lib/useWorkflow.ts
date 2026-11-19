"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getWorkflow } from "./api";
import type { WorkflowDetail } from "./types";

const TERMINAL = new Set(["completed", "failed"]);

export function useWorkflow(initialId: string | null) {
  const [workflowId, setWorkflowId] = useState<string | null>(initialId);
  const [workflow, setWorkflow] = useState<WorkflowDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);

  const refresh = useCallback(async () => {
    if (!workflowId || inFlight.current) return;
    inFlight.current = true;
    try {
      setWorkflow(await getWorkflow(workflowId));
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "查询工作流失败");
    } finally {
      inFlight.current = false;
    }
  }, [workflowId]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  useEffect(() => {
    if (!workflowId || !workflow || TERMINAL.has(workflow.status)) return;
    const timer = setInterval(refresh, 2000);
    return () => clearInterval(timer);
  }, [workflowId, workflow, refresh]);

  return { workflowId, setWorkflowId, workflow, error, refresh };
}