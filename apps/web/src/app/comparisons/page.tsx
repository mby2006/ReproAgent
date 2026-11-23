"use client";

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import AuthGate from "@/components/AuthGate";
import MissingTarget from "@/components/MissingTarget";
import Shell from "@/components/Shell";
import ComparisonClient from "./ComparisonClient";

function ComparisonRoute() {
  const id = useSearchParams().get("id");
  if (!id) return <MissingTarget />;
  return (
    <AuthGate>
      <Shell>
        <ComparisonClient comparisonId={id} />
      </Shell>
    </AuthGate>
  );
}

export default function ComparisonsPage() {
  return (
    <Suspense fallback={<div className="p-6 font-mono text-sm text-fg-3">加载中…</div>}>
      <ComparisonRoute />
    </Suspense>
  );
}
