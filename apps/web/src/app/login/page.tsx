"use client";

import SiteLink from "@/components/SiteLink";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { IconArrowLeft, LogoMark } from "@/components/icons";
import { demoLogin, isStaticSite, selfServiceAuth } from "@/lib/site";
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
  return <aside className="auth-side">
    <SiteLink href="/" className="wordmark"><LogoMark />ReproAgent</SiteLink>
    <div>
      <p className="paper-citation"><span aria-hidden="true" />LeNet-5 × MNIST</p>
      <h2>每个参数，<br />都有来处可查。</h2>
      <p>从论文原文开始，记录训练中的假设和调整，再对照实际运行的结果。</p>
      <SiteLink href="/examples/lenet-mnist#conditions" className="text-action">先看看公开实验的参数对照表</SiteLink>
    </div>
  </aside>;
}

export default function LoginPage() {
  const router = useRouter();
  const demo = demoLogin;
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

  async function signIn(mail: string, pass: string) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const { error } = await supabase.auth.signInWithPassword({ email: mail, password: pass });
      if (error) throw error;
      router.replace("/papers");
    } catch (err) {
      setError(err instanceof Error ? err.message : "登录失败");
    } finally {
      setBusy(false);
    }
  }

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    if (isStaticSite) {
      setError(null);
      setNotice("当前为静态演示版，账号与复现工作台需后端服务支持；可先体验公开实验。");
      return;
    }
    if (mode === "signin") {
      await signIn(email, password);
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      if (mode === "signup") {
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

        <form onSubmit={submit} className="card relative w-full max-w-md space-y-5 p-6 sm:p-9">
          <SiteLink href="/" className="wordmark lg:hidden"><LogoMark />ReproAgent</SiteLink>
          <div>
            <h1 className="display text-2xl font-bold">{TITLES[mode]}</h1>
          </div>

          {isStaticSite && (
            <p className="rounded-lg border border-info/40 bg-info/10 px-3 py-2 text-xs text-info">
              静态演示版未连接后端服务，登录与工作台暂未开放。
            </p>
          )}

          {demo && mode === "signin" && (
            <button
              type="button"
              disabled={busy || isStaticSite}
              onClick={() => {
                setEmail(demo.email);
                setPassword(demo.password);
                void signIn(demo.email, demo.password);
              }}
              className="btn btn-primary w-full"
            >
              {busy ? "处理中…" : "一键进入演示账号"}
            </button>
          )}

          {(mode === "signin" || mode === "signup") && selfServiceAuth && (
            <div className="flex gap-1 rounded-lg border border-line-soft bg-ink-950/60 p-1">
              {(["signin", "signup"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  aria-pressed={mode === m}
                  onClick={() => { setMode(m); setError(null); setNotice(null); }}
                  className={`tab flex-1 text-xs! ${mode === m ? "tab-active" : ""}`}
                >
                  {m === "signin" ? "登录" : "注册"}
                </button>
              ))}
            </div>
          )}

          {mode !== "update" && (
            <label className="block space-y-1.5">
              <span className="label">邮箱</span>
              <input
                type="email"
                autoComplete="email"
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
              <span className="label">密码</span>
              <input
                type="password"
                autoComplete={mode === "signin" ? "current-password" : "new-password"}
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
            <p role="alert" className="rounded-lg border border-bad/40 bg-bad/10 px-3 py-2 text-xs text-bad">
              {error}
            </p>
          )}
          {notice && (
            <p role="status" className="rounded-lg border border-ok/40 bg-ok/10 px-3 py-2 text-xs text-ok">
              {notice}
            </p>
          )}

          <button type="submit" disabled={busy} className={`btn w-full ${demo ? "btn-ghost" : "btn-primary"}`}>
            {busy ? "处理中…" : SUBMIT_LABEL[mode]}
          </button>

          {mode === "signin" && selfServiceAuth && (
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
          <SiteLink href="/examples/lenet-mnist" className="text-action lg:hidden">免登录体验公开实验</SiteLink>
        </form>
      </div>
    </main>
  );
}
