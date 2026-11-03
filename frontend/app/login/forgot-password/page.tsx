"use client";

import { FormEvent, useState } from "react";
import { useRouter } from "next/navigation";

import {
  confirmPublicPasswordRecovery,
  requestPublicPasswordRecovery,
} from "@/lib/account-api";
import { writeLocal } from "@/lib/storage";

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const USER_ID_KEY = "blink_user_id";

export default function ForgotPasswordPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [emailTouched, setEmailTouched] = useState(false);
  const [requestId, setRequestId] = useState("");
  const [code, setCode] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const normalizedEmail = email.trim().toLowerCase();
  const emailError =
    emailTouched && !EMAIL_PATTERN.test(normalizedEmail)
      ? "Email 格式不正確"
      : "";

  async function requestCode(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setEmailTouched(true);
    setError("");
    setNotice("");
    if (!EMAIL_PATTERN.test(normalizedEmail)) return;

    setSubmitting(true);
    try {
      const result = await requestPublicPasswordRecovery(normalizedEmail);
      setRequestId(result.request_id);
      setNotice("若此 Email 已註冊，驗證碼將寄到該信箱。");
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "驗證信寄送失敗，請稍後再試",
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function confirmReset(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    if (!/^\d{6}$/.test(code)) {
      setError("請輸入 6 位數驗證碼");
      return;
    }
    if (newPassword.length < 8 || !/[A-Za-z]/.test(newPassword) || !/\d/.test(newPassword)) {
      setError("會員密碼至少 8 個字元，並需同時包含英文字母與數字");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("兩次輸入的會員密碼不一致");
      return;
    }

    setSubmitting(true);
    try {
      const result = await confirmPublicPasswordRecovery(
        requestId,
        code,
        newPassword,
      );
      writeLocal(USER_ID_KEY, result.user_id);
      router.replace("/");
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : "重設密碼失敗",
      );
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <main className="login-page">
      <section className="login-window login-window-single">
        <form
          className="login-form login-form-single"
          onSubmit={requestId ? confirmReset : requestCode}
          noValidate
        >
          <div className="login-heading">
            <span className="eyebrow-badge">ACCOUNT RECOVERY</span>
            <h1>重設 <em>BLINK AI</em> 密碼</h1>
            <p className="login-description">
              使用會員 Email 驗證身分；驗證碼只能使用一次。
            </p>
          </div>

          {!requestId ? (
            <label>
              Email
              <input
                type="email"
                value={email}
                onChange={(event) => {
                  setEmail(event.target.value);
                  setError("");
                }}
                onBlur={() => setEmailTouched(true)}
                placeholder="請輸入會員 Email"
                autoComplete="email"
                aria-invalid={Boolean(emailError)}
                required
              />
              {emailError ? (
                <span className="login-field-hint is-error">{emailError}</span>
              ) : null}
            </label>
          ) : (
            <>
              <label>
                6 位數驗證碼
                <input
                  inputMode="numeric"
                  maxLength={6}
                  value={code}
                  onChange={(event) => {
                    setCode(event.target.value.replace(/\D/g, ""));
                    setError("");
                  }}
                  autoComplete="one-time-code"
                  required
                />
              </label>
              <label>
                新密碼
                <input
                  type="password"
                  value={newPassword}
                  onChange={(event) => setNewPassword(event.target.value)}
                  autoComplete="new-password"
                  maxLength={72}
                  required
                />
                <span className="login-field-hint">
                  至少 8 個字元，需同時包含英文字母與數字
                </span>
              </label>
              <label>
                再次輸入新密碼
                <input
                  type="password"
                  value={confirmPassword}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                  autoComplete="new-password"
                  maxLength={72}
                  required
                />
              </label>
            </>
          )}

          {error ? <p className="login-error" role="alert">{error}</p> : null}
          {notice ? <p className="account-security-success">{notice}</p> : null}

          <button className="glossy-primary" type="submit" disabled={submitting}>
            {submitting
              ? "處理中…"
              : requestId
                ? "驗證並重設密碼"
                : "寄送驗證碼"}
          </button>

          {requestId ? (
            <button
              type="button"
              className="text-button"
              disabled={submitting}
              onClick={() => {
                setRequestId("");
                setCode("");
                setError("");
                setNotice("");
              }}
            >
              重新輸入 Email
            </button>
          ) : null}

          <button
            type="button"
            className="text-button"
            onClick={() => router.push("/login")}
          >
            返回登入
          </button>
        </form>
      </section>
    </main>
  );
}
