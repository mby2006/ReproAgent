"use client";

import { useParams } from "next/navigation";
import { Suspense } from "react";
import PlanClient from "./PlanClient";

function ParamBridge() {
  const params = useParams<{ id: string }>();
  if (!params?.id) {
    return <div className="p-6 font-mono text-sm text-fg-3">加载中…</div>;
  }
  return <PlanClient experimentId={params.id} />;
}

export default function ExperimentPage() {
  return (
    <Suspense fallback={<div className="p-6 font-mono text-sm text-fg-3">加载中…</div>}>
      <ParamBridge />
    </Suspense>
  );
}