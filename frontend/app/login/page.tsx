"use client";

import Link from "next/link";
import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";

import { browserMemberAuthUrl } from "@/lib/api-origin";

type Mode = "login" | "register";
type Provider = "google" | "line";

function safeNextPath(): string {
  if (typeof window === "undefined") return "/";
  const candidate = new URLSearchParams(window.location.search).get("next") || "/";
  return candidate.startsWith("/") && !candidate.startsWith("//") ? candidate : "/";
}

async function readError(response: Response): Promise<string> {
  try {
    const payload = (await response.json()) as { detail?: unknown };
    if (response.status === 401) return "帳號或密碼不正確";
    if (response.status === 409) return "這個帳號已有人使用";
    if (response.status === 429) return "嘗試次數過多，請稍後再試";
    if (typeof payload.detail === "string" && payload.detail.trim()) {
      return payload.detail;
    }
  } catch {
    // Use the stable fallback below when the API is temporarily unavailable.
  }
  return "登入服務暫時無法使用，請稍後再試";
}

export default function LoginPage() {
  const router = useRouter();
  const [mode, setMode] = useState<Mode>("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  function changeMode(nextMode: Mode) {
    setMode(nextMode);
    setPassword("");
    setConfirmPassword("");
    setError("");
  }

  function startProviderLogin(provider: Provider) {
    const nextPath = safeNextPath();
    sessionStorage.setItem("blink_post_login_redirect", nextPath);
    const next = encodeURIComponent(nextPath);
    window.location.assign(`${browserMemberAuthUrl(`/${provider}/login`)}?next=${next}`);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");

    const normalizedUsername = username.trim();
    if (!normalizedUsername) {
      setError("請輸入會員帳號");
      return;
    }
    if (password.length < 8) {
      setError("會員密碼至少需要 8 個字元");
      return;
    }
    if (mode === "register" && password !== confirmPassword) {
      setError("兩次輸入的密碼不一致");
      return;
    }

    setSubmitting(true);
    try {
      const response = await fetch(
        browserMemberAuthUrl(`/password/${mode}`),
        {
          method: "POST",
          credentials: "include",
          cache: "no-store",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username: normalizedUsername, password }),
        },
      );
      if (!response.ok) throw new Error(await readError(response));

      window.dispatchEvent(new Event("blink-auth-updated"));
      router.replace(safeNextPath());
      router.refresh();
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "登入服務暫時無法使用，請稍後再試",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="login-page">
      <section className="login-window">
        <form className="login-form" onSubmit={submit} noValidate>
          <div className="login-heading">
            <span className="eyebrow-badge">WELCOME TO BLINK</span>
            <h1>
              {mode === "login" ? "登入" : "加入"} <em>BLINK AI</em>
            </h1>
            <p className="login-description">
              {mode === "login"
                ? "登入後即可保存膚況紀錄、偏好與每一次對話。"
                : "建立會員帳號，讓你的美麗紀錄不遺失。"}
            </p>
          </div>

          <div className="login-mode-switch" role="group" aria-label="登入或註冊">
            <button
              type="button"
              className={mode === "login" ? "is-active" : ""}
              aria-pressed={mode === "login"}
              onClick={() => changeMode("login")}
            >
              登入
            </button>
            <button
              type="button"
              className={mode === "register" ? "is-active" : ""}
              aria-pressed={mode === "register"}
              onClick={() => changeMode("register")}
            >
              註冊
            </button>
          </div>

          <label>
            會員帳號
            <input
              value={username}
              onChange={(event) => {
                setUsername(event.target.value);
                setError("");
              }}
              autoComplete="username"
              maxLength={40}
              placeholder="請輸入會員帳號"
              required
            />
          </label>

          <label>
            會員密碼
            <input
              type="password"
              value={password}
              onChange={(event) => {
                setPassword(event.target.value);
                setError("");
              }}
              autoComplete={mode === "login" ? "current-password" : "new-password"}
              maxLength={72}
              placeholder="至少 8 個字元"
              required
            />
          </label>

          {mode === "register" ? (
            <label>
              再次輸入密碼
              <input
                type="password"
                value={confirmPassword}
                onChange={(event) => {
                  setConfirmPassword(event.target.value);
                  setError("");
                }}
                autoComplete="new-password"
                maxLength={72}
                required
              />
            </label>
          ) : null}

          {error ? <p className="login-error" role="alert">{error}</p> : null}

          <button className="glossy-primary" type="submit" disabled={submitting}>
            {submitting
              ? "處理中…"
              : mode === "login"
                ? "登入 BLINK AI"
                : "建立會員帳號"}
          </button>

          {mode === "login" ? (
            <Link className="text-button forgot-password-button" href="/login/forgot-password">
              忘記密碼？
            </Link>
          ) : null}

          <div className="login-divider"><span>或使用快速登入</span></div>

          <div className="login-provider-actions">
            <button type="button" onClick={() => startProviderLogin("google")}>
              Google
            </button>
            <button type="button" onClick={() => startProviderLogin("line")}>
              LINE
            </button>
          </div>

          <Link className="text-button" href="/">先回首頁</Link>
        </form>
      </section>
    </main>
  );
}
