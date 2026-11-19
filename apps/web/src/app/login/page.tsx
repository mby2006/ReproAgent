"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { supabase } from "@/lib/supabase";

type Mode = "signin" | "signup" | "reset" | "update";

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

  const titles: Record<Mode, string> = {
    signin: "ReproAgent 登录",
    signup: "ReproAgent 注册",
    reset: "重置密码",
    update: "设置新密码",
  };
  const submitLabel: Record<Mode, string> = {
    signin: "登录",
    signup: "注册",
    reset: "发送重置邮件",
    update: "保存新密码",
  };

  return (
    <main className="flex flex-1 items-center justify-center p-6">
      <form
        onSubmit={submit}
        className="w-full max-w-sm space-y-4 rounded-xl border border-zinc-800 bg-zinc-900 p-6"
      >
        <h1 className="text-xl font-semibold">{titles[mode]}</h1>
        {(mode === "signin" || mode === "signup") && (
          <div className="flex gap-2 text-sm">
            <button
              type="button"
              onClick={() => setMode("signin")}
              className={`rounded px-3 py-1 ${mode === "signin" ? "bg-zinc-700" : "text-zinc-400"}`}
            >
              登录
            </button>
            <button
              type="button"
              onClick={() => setMode("signup")}
              className={`rounded px-3 py-1 ${mode === "signup" ? "bg-zinc-700" : "text-zinc-400"}`}
            >
              注册
            </button>
          </div>
        )}
        {mode !== "update" && (
          <input
            type="email"
            required
            placeholder="邮箱"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm"
          />
        )}
        {mode !== "reset" && (
          <input
            type="password"
            required
            minLength={6}
            placeholder={mode === "update" ? "新密码（至少 6 位）" : "密码（至少 6 位）"}
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm"
          />
        )}
        {error && <p className="text-sm text-red-400">{error}</p>}
        {notice && <p className="text-sm text-emerald-400">{notice}</p>}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded bg-emerald-600 py-2 text-sm font-medium hover:bg-emerald-500 disabled:opacity-50"
        >
          {busy ? "处理中…" : submitLabel[mode]}
        </button>
        {mode === "signin" && (
          <button
            type="button"
            onClick={() => {
              setMode("reset");
              setError(null);
              setNotice(null);
            }}
            className="w-full text-center text-xs text-zinc-500 hover:text-zinc-300"
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
            className="w-full text-center text-xs text-zinc-500 hover:text-zinc-300"
          >
            返回登录
          </button>
        )}
      </form>
    </main>
  );
}