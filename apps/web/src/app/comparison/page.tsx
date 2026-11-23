"use client";

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";
import AuthGate from "@/components/AuthGate";
import ComparisonSetup from "./ComparisonSetup";
import MissingTarget from "@/components/MissingTarget";
import Shell from "@/components/Shell";

function ComparisonSetupRoute() {
  const id = useSearchParams().get("id");
  if (!id) return <MissingTarget />;
  return (
    <AuthGate>
      <Shell>
        <ComparisonSetup paperId={id} />
      </Shell>
    </AuthGate>
  );
}

export default function ComparisonPage() {
  return (
    <Suspense fallback={<div className="p-6 font-mono text-sm text-fg-3">加载中…</div>}>
      <ComparisonSetupRoute />
    </Suspense>
  );
}
