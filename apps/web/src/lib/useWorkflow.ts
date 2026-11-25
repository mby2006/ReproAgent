"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getWorkflow } from "./api";
import { startVisiblePolling } from "./polling";
import { isWorkflowTerminal } from "./ui";
import type { WorkflowDetail } from "./types";

export function useWorkflow(initialId: string | null) {
  const [workflowId, setWorkflowId] = useState<string | null>(initialId);
  const [workflow, setWorkflow] = useState<WorkflowDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef<string | null>(null);
  const requestVersion = useRef(0);

  const refresh = useCallback(async () => {
    if (!workflowId || inFlight.current === workflowId) return;
    const version = ++requestVersion.current;
    inFlight.current = workflowId;
    try {
      const next = await getWorkflow(workflowId);
      if (next && version === requestVersion.current) {
        setWorkflow(next);
        setError(null);
      }
    } catch (err) {
      if (version === requestVersion.current) {
        setError(err instanceof Error ? err.message : "查询工作流失败");
      }
    } finally {
      if (inFlight.current === workflowId) inFlight.current = null;
    }
  }, [workflowId]);

  useEffect(() => {
    setWorkflow(null);
    void refresh();
  }, [refresh]);

  const workflowActive = workflowId != null && workflow != null && !isWorkflowTerminal(workflow.status);

  useEffect(() => {
    if (!workflowActive) return;
    return startVisiblePolling(() => refresh(), 2000);
  }, [workflowActive, refresh]);

  return { workflowId, setWorkflowId, workflow, error, refresh };
}