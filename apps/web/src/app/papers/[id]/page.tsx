"use client";

import { useParams } from "next/navigation";
import { Suspense } from "react";
import PaperClient from "./PaperClient";

function ParamBridge() {
  const params = useParams<{ id: string }>();
  if (!params?.id) {
    return <div className="p-6 text-zinc-500">加载中…</div>;
  }
  return <PaperClient paperId={params.id} />;
}

export default function PaperPage() {
  return (
    <Suspense fallback={<div className="p-6 text-zinc-500">加载中…</div>}>
      <ParamBridge />
    </Suspense>
  );
}