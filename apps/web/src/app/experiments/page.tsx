"use client";

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import MissingTarget from "@/components/MissingTarget";
import PlanClient from "./PlanClient";

function ExperimentRoute() {
  const id = useSearchParams().get("id");
  return id ? <PlanClient experimentId={id} /> : <MissingTarget />;
}

export default function ExperimentsPage() {
  return (
    <Suspense fallback={<div className="p-6 font-mono text-sm text-fg-3">加载中…</div>}>
      <ExperimentRoute />
    </Suspense>
  );
}
