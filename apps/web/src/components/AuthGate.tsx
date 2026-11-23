"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { supabase } from "@/lib/supabase";
import { LogoMark } from "./icons";

export default function AuthGate({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const [ready, setReady] = useState(false);

  useEffect(() => {
    supabase.auth.getSession().then(({ data }) => {
      if (!data.session) router.replace("/login");
      else setReady(true);
    });
    const { data: sub } = supabase.auth.onAuthStateChange((_event, session) => {
      if (!session) router.replace("/login");
    });
    return () => sub.subscription.unsubscribe();
  }, [router]);

  if (!ready) {
    return (
      <div className="flex min-h-dvh flex-col items-center justify-center gap-4 bg-ink-950">

        <LogoMark className="pulse-dot h-12 w-12" />
        <p role="status" className="label">正在确认登录状态…</p>
      </div>
    );
  }
  return <>{children}</>;
}
