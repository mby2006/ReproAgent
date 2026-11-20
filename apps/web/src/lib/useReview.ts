"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import type { ConfirmationSource, OrdinaryReviewMode, ReviewMode } from "./types";

// Only persisted consent makes AUTO decisions effective; selecting a mode never grants it.
export function hasDelegation(value?: {
  review_mode?: ReviewMode;
  delegated_at?: string | null;
  delegated_by?: string | null;
}) {
  return value?.review_mode === "delegated" && !!value.delegated_at && !!value.delegated_by;
}

export function isReviewHandled(field: { human_confirmed: boolean; confirmed_by?: ConfirmationSource | null }, delegated: boolean) {
  return field.human_confirmed || (delegated && field.confirmed_by === "auto");
}

export function ordinaryReviewMode(mode: ReviewMode): OrdinaryReviewMode {
  return mode === "delegated" ? "assisted" : mode;
}

// Unknown paths remain critical even if the server proposes preselection.
const ROUTINE_CARD_PATHS = new Set([
  "hyperparameters.weight_decay", "hyperparameters.seed",
  "hyperparameters.batch_size", "hyperparameters.epochs",
  "data_preprocessing.normalize", "data_preprocessing.normalization",
  "data_preprocessing.to_tensor",
]);

export function isRoutineCardPath(path: string) {
  return ROUTINE_CARD_PATHS.has(path);
}

export function useReviewMode(key: string, stored?: ReviewMode) {
  const initial = stored ?? "assisted";
  const [state, setState] = useState({ key, mode: initial as ReviewMode });
  if (state.key !== key) setState({ key, mode: initial });
  return {
    mode: state.key === key ? state.mode : initial,
    setMode: (mode: ReviewMode) => setState({ key, mode }),
  };
}

export function useReviewSelection(key: string, paths: string[], defaults: string[]) {
  const [state, setState] = useState<{ key: string; choices: Record<string, boolean> }>({ key, choices: {} });
  if (state.key !== key) setState({ key, choices: {} });
  const choices = state.key === key ? state.choices : {};
  return {
    selectedPaths: paths.filter((path) => choices[path] ?? defaults.includes(path)),
    toggle: (path: string, checked: boolean) => setState((previous) => ({
      key,
      choices: { ...(previous.key === key ? previous.choices : {}), [path]: checked },
    })),
  };
}

// Each version owns cancellation and one automatic attempt; failures require an explicit retry.
export function useReviewRequest<T>(
  key: string,
  automatic: boolean,
  request: (signal: AbortSignal) => Promise<T>,
) {
  const scope = useMemo(() => ({
    key, attempted: false, controller: null as AbortController | null,
  }), [key]);
  const [state, setState] = useState<{
    scope: typeof scope;
    data: T | null;
    loading: boolean;
    error: string | null;
  }>({ scope, data: null, loading: false, error: null });

  const load = useCallback(async () => {
    if (!key || scope.controller) return;
    scope.attempted = true;
    const controller = new AbortController();
    scope.controller = controller;
    setState((previous) => ({
      scope, data: previous.scope === scope ? previous.data : null, loading: true, error: null,
    }));
    try {
      const data = await request(controller.signal);
      if (!controller.signal.aborted) setState({ scope, data, loading: false, error: null });
    } catch (error) {
      if (!controller.signal.aborted) setState((previous) => ({
        ...previous, loading: false, error: error instanceof Error ? error.message : "AI 辅助暂不可用",
      }));
    } finally {
      if (scope.controller === controller) scope.controller = null;
    }
  }, [key, request, scope]);

  useEffect(() => () => {
    scope.controller?.abort();
    scope.controller = null;
    scope.attempted = false;
  }, [scope]);

  useEffect(() => {
    if (automatic && !scope.attempted) void load();
  }, [automatic, load, scope]);

  return {
    data: state.scope === scope ? state.data : null,
    loading: state.scope === scope && state.loading,
    error: state.scope === scope ? state.error : null,
    load,
  };
}
