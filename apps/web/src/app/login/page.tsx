"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { IconArrowLeft, IconLock, IconQuote, LogoMark } from "@/components/icons";
import { supabase } from "@/lib/supabase";

type Mode = "signin" | "signup" | "reset" | "update";

const TITLES: Record<Mode, string> = {
  signin: "登录工作台",
  signup: "创建账户",
  reset: "重置密码",
  update: "设置新密码",
};

const SUBMIT_LABEL: Record<Mode, string> = {
  signin: "登录",
  signup: "注册",
  reset: "发送重置邮件",
  update: "保存新密码",
};

function BrandPanel() {
  return (
    <div className="relative hidden overflow-hidden border-r border-line-soft bg-ink-900/70 lg:flex lg:flex-col lg:justify-between lg:p-10">
      <div className="ambient absolute!" />
      <div className="relative">
        <Link href="/" className="flex items-center gap-3">
          <LogoMark className="h-9 w-9" />
          <span className="display text-lg font-bold tracking-wide">
            REPRO<span className="text-brand">/</span>AGENT
          </span>
        </Link>
        <p className="label mt-2">Evidence Lab · Phase 1</p>
      </div>

      <div className="relative my-10">
        <div className="card relative mx-auto max-w-sm overflow-hidden p-6">
          <div className="pointer-events-none absolute inset-x-0 h-10 animate-scan bg-gradient-to-b from-transparent via-brand/12 to-transparent" />
          <p className="label mb-4 flex items-center gap-2">
            <IconQuote className="h-3.5 w-3.5 text-brand" /> Reproduction Card
          </p>
          <div className="space-y-3 font-mono text-[11px]">
            {[
              ["method", "paper_explicit", "bg-ok"],
              ["learning_rate", "inferred", "bg-warn"],
              ["weight_decay", "missing", "bg-fg-3"],
            ].map(([field, status, dot]) => (
              <div key={field} className="flex items-center justify-between gap-3 border-b border-line-soft pb-2 last:border-0 last:pb-0">
                <span className="text-fg-2">{field}</span>
                <span className="flex items-center gap-2 text-fg-3">
                  <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />
                  {status}
                </span>
              </div>
            ))}
          </div>
          <p className="mt-4 text-[11px] leading-relaxed text-fg-3">
            每个字段都带原文引文与页码；推断值需人工确认，缺失信息绝不伪造。
          </p>
        </div>
      </div>

      <div className="relative space-y-2">
        <p className="display text-xl font-semibold leading-snug">
          从论文到可追溯的
          <br />
          复现报告。
        </p>
        <p className="flex items-center gap-2 text-xs text-fg-3">
          <IconLock className="h-3.5 w-3.5 text-scale" />
          全表 RLS 行级安全 · 无密钥沙箱执行
        </p>
      </div>
    </div>
  );
}

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [mode, setMode] = useState<Mode>("signin");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    // supabase-js may consume the recovery hash before this listener mounts,
    // so also detect it directly on arrival.
    if (window.location.hash.includes("type=recovery")) {
      setMode("update");
      setNotice("请设置新密码。");
    }
    const {
      data: { subscription },
    } = supabase.auth.onAuthStateChange((event) => {
      if (event === "PASSWORD_RECOVERY") {
        setMode("update");
        setError(null);
        setNotice("请设置新密码。");
      }
    });
    return () => subscription.unsubscribe();
  }, []);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (mode === "signin") {
        const { error } = await supabase.auth.signInWithPassword({ email, password });
        if (error) throw error;
        router.replace("/papers");
      } else if (mode === "signup") {
        const { data, error } = await supabase.auth.signUp({ email, password });
        if (error) throw error;
        if (data.session) router.replace("/papers");
        else setNotice("注册成功，请查收确认邮件后再登录。");
      } else if (mode === "reset") {
        const { error } = await supabase.auth.resetPasswordForEmail(email, {
          redirectTo: `${window.location.origin}/login`,
        });
        if (error) throw error;
        setNotice("重置邮件已发送，请查收并点击链接设置新密码。");
      } else {
        const { error } = await supabase.auth.updateUser({ password });
        if (error) throw error;
        router.replace("/papers");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="grid min-h-dvh lg:grid-cols-[1fr_1.05fr]">
      <BrandPanel />

      <div className="relative flex items-center justify-center p-6">
        <div className="ambient" />
        <form onSubmit={submit} className="card rise relative w-full max-w-sm space-y-4 p-7">
          <div className="mb-1 flex items-center gap-3 lg:hidden">
            <LogoMark className="h-8 w-8" />
            <span className="display text-base font-bold tracking-wide">
              REPRO<span className="text-brand">/</span>AGENT
            </span>
          </div>
          <div>
            <p className="label mb-1.5">Access · 账户</p>
            <h1 className="display text-2xl font-bold">{TITLES[mode]}</h1>
          </div>

          {(mode === "signin" || mode === "signup") && (
            <div className="flex gap-1 rounded-lg border border-line-soft bg-ink-950/60 p-1">
              {(["signin", "signup"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setMode(m)}
                  className={`tab flex-1 text-xs! ${mode === m ? "tab-active" : ""}`}
                >
                  {m === "signin" ? "登录" : "注册"}
                </button>
              ))}
            </div>
          )}

          {mode !== "update" && (
            <label className="block space-y-1.5">
              <span className="label">Email</span>
              <input
                type="email"
                required
                placeholder="you@lab.edu"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="input"
              />
            </label>
          )}
          {mode !== "reset" && (
            <label className="block space-y-1.5">
              <span className="label">Password</span>
              <input
                type="password"
                required
                minLength={6}
                placeholder={mode === "update" ? "新密码（至少 6 位）" : "密码（至少 6 位）"}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="input"
              />
            </label>
          )}

          {error && (
            <p className="rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-xs text-bad">
              {error}
            </p>
          )}
          {notice && (
            <p className="rounded-lg border border-ok/40 bg-ok/10 px-3 py-2 text-xs text-ok">
              {notice}
            </p>
          )}

          <button type="submit" disabled={busy} className="btn btn-primary w-full">
            {busy ? "处理中…" : SUBMIT_LABEL[mode]}
          </button>

          {mode === "signin" && (
            <button
              type="button"
              onClick={() => {
                setMode("reset");
                setError(null);
                setNotice(null);
              }}
              className="link-quiet w-full text-center"
            >
              忘记密码？
            </button>
          )}
          {(mode === "reset" || mode === "update") && (
            <button
              type="button"
              onClick={() => {
                setMode("signin");
                setError(null);
                setNotice(null);
              }}
              className="link-quiet flex w-full items-center justify-center gap-1.5"
            >
              <IconArrowLeft className="h-3.5 w-3.5" /> 返回登录
            </button>
          )}
        </form>
      </div>
    </main>
  );
}