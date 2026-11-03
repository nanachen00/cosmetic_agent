"use client";

import { FormEvent, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { Icon } from "@/components/Icon";
import { browserMemberAuthUrl } from "@/lib/api-origin";

const USERNAME_PATTERN = /^[a-z0-9._]{4,20}$/;

type OnboardingAccount = {
  user_id: string;
  username?: string | null;
  display_name?: string | null;
  email?: string | null;
  provider: "google" | "line";
};

function normalizeUsername(value: string): string {
  return value.trim().toLowerCase().replace(/^@/, "");
}

function validateUsername(value: string): string {
  const normalized = normalizeUsername(value);
  if (!USERNAME_PATTERN.test(normalized)) {
    return "請輸入 4–20 個字元，只能使用小寫英文字母、數字、句點與底線";
  }
  if (normalized.startsWith(".") || normalized.endsWith(".")) {
    return "會員帳號不能以句點開頭或結尾";
  }
  if (normalized.includes("..")) {
    return "會員帳號不能包含連續句點";
  }
  return "";
}

async function readErrorMessage(response: Response): Promise<string> {
  try {
    const result = (await response.json()) as { detail?: string };
    return result.detail || "操作失敗，請稍後再試";
  } catch {
    return "操作失敗，請稍後再試";
  }
}

function saveLogin(account: OnboardingAccount, username: string): void {
  window.dispatchEvent(new Event("blink-auth-updated"));
}

export default function SetupUsernamePage() {
  const router = useRouter();
  const [account, setAccount] = useState<OnboardingAccount | null>(null);
  const [username, setUsername] = useState("");
  const [touched, setTouched] = useState(false);
  const [checking, setChecking] = useState(false);
  const [available, setAvailable] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;

    async function loadAccount() {
      try {
        const response = await fetch(browserMemberAuthUrl("/onboarding/status"), {
          credentials: "include",
        });
        if (!response.ok) {
          throw new Error(await readErrorMessage(response));
        }
        const data = (await response.json()) as OnboardingAccount;
        if (!active) {
          return;
        }
        if (data.username) {
          saveLogin(data, data.username);
          router.replace("/");
          return;
        }
        setAccount(data);
      } catch (loadError) {
        if (active) {
          setError(
            loadError instanceof Error
              ? loadError.message
              : "快速登入狀態確認失敗，請重新登入",
          );
        }
      } finally {
        if (active) {
          setLoading(false);
        }
      }
    }

    void loadAccount();
    return () => {
      active = false;
    };
  }, [router]);

  const usernameError = touched ? validateUsername(username) : "";
  const helperMessage = usernameError
    ? usernameError
    : checking
      ? "正在檢查帳號是否可使用…"
      : available === true
        ? "這個會員帳號可以使用"
        : available === false
          ? "這個會員帳號已被使用"
          : "4–20 個字元，可使用小寫英文字母、數字、句點與底線";
  const helperIsError = Boolean(usernameError) || available === false;

  async function checkAvailability(): Promise<boolean> {
    setTouched(true);
    const validationError = validateUsername(username);
    if (validationError) {
      setAvailable(null);
      return false;
    }

    setChecking(true);
    try {
      const normalized = normalizeUsername(username);
      const response = await fetch(
        `${browserMemberAuthUrl("/username/availability")}?username=${encodeURIComponent(normalized)}`,
        { credentials: "include" },
      );
      if (!response.ok) {
        throw new Error(await readErrorMessage(response));
      }
      const data = (await response.json()) as { available?: boolean };
      const isAvailable = data.available === true;
      setAvailable(isAvailable);
      return isAvailable;
    } catch (availabilityError) {
      setAvailable(null);
      setError(
        availabilityError instanceof Error
          ? availabilityError.message
          : "暫時無法檢查會員帳號",
      );
      return false;
    } finally {
      setChecking(false);
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    if (!(await checkAvailability()) || !account) {
      return;
    }

    setSubmitting(true);
    try {
      const normalized = normalizeUsername(username);
      const response = await fetch(browserMemberAuthUrl("/onboarding/username"), {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: normalized }),
      });
      if (!response.ok) {
        throw new Error(await readErrorMessage(response));
      }
      const data = (await response.json()) as {
        user_id: string;
        username: string;
      };
      saveLogin({ ...account, user_id: data.user_id }, data.username);
      router.replace("/");
    } catch (submitError) {
      setError(
        submitError instanceof Error
          ? submitError.message
          : "設定會員帳號失敗",
      );
    } finally {
      setSubmitting(false);
    }
  }

  const providerName = account?.provider === "line" ? "LINE" : "Google";

  return (
    <main className="login-page">
      <section className="login-window login-window-single">
        <form
          className="login-form login-form-single"
          onSubmit={submit}
          noValidate
        >
          <div className="login-heading">
            <span className="eyebrow-badge">ACCOUNT SETUP</span>
            <h1>
              設定你的 <em>會員帳號</em>
            </h1>
            <p className="login-description">
              {loading
                ? "正在確認快速登入資料…"
                : account
                  ? `已完成 ${providerName} 驗證，請設定日後使用的唯一會員帳號。`
                  : "這次快速登入已失效，請返回登入頁重新驗證。"}
            </p>
          </div>

          {account ? (
            <>
              <label>
                會員帳號
                <input
                  type="text"
                  value={username}
                  onChange={(event) => {
                    setUsername(event.target.value.toLowerCase().replace(/^@/, ""));
                    setAvailable(null);
                    setError("");
                  }}
                  onBlur={() => void checkAvailability()}
                  placeholder="輸入會員帳號"
                  autoComplete="username"
                  autoCapitalize="none"
                  spellCheck={false}
                  maxLength={20}
                  aria-invalid={helperIsError}
                  aria-describedby="setup-username-help"
                  required
                />
                <span
                  id="setup-username-help"
                  className={`login-field-hint${helperIsError ? " is-error" : ""}`}
                >
                  {helperMessage}
                </span>
              </label>

              {error ? (
                <p className="login-error" role="alert">
                  {error}
                </p>
              ) : null}

              <button
                className="glossy-primary"
                type="submit"
                disabled={submitting || checking}
              >
                {submitting ? "設定中…" : "完成並進入 BLINK AI"}
              </button>
            </>
          ) : loading ? null : (
            <>
              {error ? (
                <p className="login-error" role="alert">
                  {error}
                </p>
              ) : null}
              <button
                className="glossy-primary"
                type="button"
                onClick={() => router.replace("/login")}
              >
                返回登入
              </button>
            </>
          )}

          <div className="privacy-note">
            <Icon name="lock" />
            Google／LINE 僅負責身分驗證，不會取得或保存你的登入密碼。
          </div>
        </form>
      </section>
    </main>
  );
}
