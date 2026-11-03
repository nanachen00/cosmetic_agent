"use client";

import { FormEvent, useEffect, useState } from "react";
import {
  changeAccountUsername,
  changeAccountPassword,
  confirmPasswordReset,
  getAccountSecurity,
  getProviderLinkUrl,
  requestPasswordResetCode,
  setInitialPassword,
  unlinkAccountProvider,
  verifyCurrentPassword,
  type AccountSecurityState,
  type AuthProvider,
} from "@/lib/account-api";
import { writeLocal } from "@/lib/storage";

type PasswordForm = {
  currentPassword: string;
  newPassword: string;
  confirmPassword: string;
};

const EMPTY_PASSWORD_FORM: PasswordForm = {
  currentPassword: "",
  newPassword: "",
  confirmPassword: "",
};

function providerLabel(provider: AuthProvider): string {
  return provider === "google" ? "Google" : "LINE";
}

const LINK_ERROR_MESSAGES: Record<string, string> = {
  oauth_link_failed: "帳號綁定失敗，請稍後再試。",
};

export function AccountSecurityPanel() {
  const [account, setAccount] = useState<AccountSecurityState | null>(null);
  const [loading, setLoading] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [usernameOpen, setUsernameOpen] = useState(false);
  const [usernameDraft, setUsernameDraft] = useState("");
  const [providerToUnlink, setProviderToUnlink] = useState<AuthProvider | null>(null);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [currentPasswordVerified, setCurrentPasswordVerified] = useState(false);
  const [resetChallengeId, setResetChallengeId] = useState("");
  const [resetCode, setResetCode] = useState("");
  const [maskedEmail, setMaskedEmail] = useState("");
  const [form, setForm] = useState<PasswordForm>(EMPTY_PASSWORD_FORM);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    let cancelled = false;
    getAccountSecurity()
      .then((result) => {
        if (!cancelled) {
          setAccount(result);
          setUsernameDraft(result.username || "");
        }
      })
      .catch((requestError) => {
        if (!cancelled) {
          setError(
            requestError instanceof Error
              ? requestError.message
              : "無法讀取會員資料",
          );
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const linked = params.get("linked");
    const linkError = params.get("link_error");
    if (linked === "google" || linked === "line") {
      setNotice(`${providerLabel(linked)} 綁定完成。`);
    } else if (linkError) {
      setError(LINK_ERROR_MESSAGES[linkError] ?? "帳號綁定失敗，請稍後再試。");
    }
    if (linked || linkError) {
      params.delete("linked");
      params.delete("link_error");
      const search = params.toString();
      window.history.replaceState(
        null,
        "",
        `${window.location.pathname}${search ? `?${search}` : ""}${window.location.hash}`,
      );
    }
  }, []);

  useEffect(() => {
    if (!passwordOpen && !usernameOpen && !providerToUnlink) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !submitting) {
        setPasswordOpen(false);
        setUsernameOpen(false);
        setProviderToUnlink(null);
        setError("");
        setNotice("");
      }
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", closeOnEscape);
    };
  }, [passwordOpen, providerToUnlink, usernameOpen, submitting]);

  function closeUsernameDialog() {
    if (submitting) return;
    setUsernameOpen(false);
    setUsernameDraft(account?.username || "");
    setError("");
  }

  function closePasswordDialog() {
    if (submitting) return;
    setPasswordOpen(false);
    setError("");
    setNotice("");
  }

  function updateField(key: keyof PasswordForm, value: string) {
    setForm((current) => ({ ...current, [key]: value }));
    setError("");
    setNotice("");
    if (key === "currentPassword") setCurrentPasswordVerified(false);
  }

  function linkProvider(provider: AuthProvider) {
    setError("");
    setNotice("");
    window.location.href = getProviderLinkUrl(provider);
  }

  function handleProviderClick(provider: AuthProvider, linked: boolean) {
    if (!linked) {
      linkProvider(provider);
      return;
    }
    setProviderToUnlink(provider);
    setError("");
    setNotice("");
  }

  function closeProviderDialog() {
    if (submitting) return;
    setProviderToUnlink(null);
    setError("");
  }

  async function confirmProviderUnlink() {
    if (!providerToUnlink) return;
    const provider = providerToUnlink;
    setSubmitting(true);
    setError("");
    setNotice("");
    try {
      const updated = await unlinkAccountProvider(provider);
      setAccount(updated);
      setProviderToUnlink(null);
      window.dispatchEvent(new Event("blink-auth-updated"));
      setNotice(`${providerLabel(provider)} 已解除綁定。`);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : `無法解除 ${providerLabel(provider)} 綁定`,
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function submitUsername(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!account) return;

    const normalized = usernameDraft.trim().toLowerCase().replace(/^@/, "");
    if (!/^[a-z0-9._]{4,20}$/.test(normalized)) {
      setError("會員帳號須為 4–20 個字元，只能使用小寫英文字母、數字、句點與底線");
      return;
    }
    if (normalized === account.username.toLowerCase()) {
      setError("新帳號名稱與目前相同");
      return;
    }

    setSubmitting(true);
    setError("");
    setNotice("");
    try {
      const updated = await changeAccountUsername(normalized);
      setAccount(updated);
      setUsernameDraft(updated.username);
      writeLocal("blink_username", updated.username);
      window.dispatchEvent(new Event("blink-auth-updated"));
      setUsernameOpen(false);
      setNotice("會員帳號已更新；7 天後才能再次修改。");
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "無法修改會員帳號",
      );
    } finally {
      setSubmitting(false);
    }
  }

  const usernameAvailableAt = account?.username_change_available_at
    ? new Date(account.username_change_available_at)
    : null;
  const usernameChangeLocked = Boolean(
    usernameAvailableAt && usernameAvailableAt.getTime() > Date.now(),
  );
  const usernameAvailableLabel = usernameAvailableAt
    ? usernameAvailableAt.toLocaleString("zh-TW", {
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
      })
    : "";
  const unlinkWouldRemoveLastLogin = Boolean(
    providerToUnlink &&
    account &&
    account.providers.length <= 1 &&
    !account.password_set,
  );

  async function verifyPassword() {
    if (!form.currentPassword) {
      setError("請輸入原密碼");
      return;
    }
    setSubmitting(true);
    setError("");
    try {
      await verifyCurrentPassword(form.currentPassword);
      setCurrentPasswordVerified(true);
      setNotice("原密碼驗證成功，請設定新密碼。");
    } catch (requestError) {
      setCurrentPasswordVerified(false);
      setError(
        requestError instanceof Error ? requestError.message : "原密碼驗證失敗",
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function submitPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!account) return;
    if (account.password_set && !currentPasswordVerified) {
      setError("請先驗證原密碼");
      return;
    }
    if (form.newPassword.length < 8) {
      setError("會員密碼至少需要 8 個字元");
      return;
    }
    if (!/[A-Za-z]/.test(form.newPassword) || !/\d/.test(form.newPassword)) {
      setError("會員密碼需同時包含英文字母與數字");
      return;
    }
    if (form.newPassword !== form.confirmPassword) {
      setError("兩次輸入的會員密碼不一致");
      return;
    }

    setSubmitting(true);
    setError("");
    setNotice("");
    try {
      if (account.password_set) {
        await changeAccountPassword(form.currentPassword, form.newPassword);
        setNotice("會員密碼已更新，其他裝置已登出。");
      } else {
        await setInitialPassword(form.newPassword);
        setAccount((current) =>
          current ? { ...current, password_set: true } : current,
        );
        setNotice("會員密碼設定完成。");
      }
      setForm(EMPTY_PASSWORD_FORM);
      setCurrentPasswordVerified(false);
      setPasswordOpen(false);
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : "密碼操作失敗",
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function requestResetCode() {
    setSubmitting(true);
    setError("");
    setNotice("");
    try {
      const result = await requestPasswordResetCode();
      setResetChallengeId(result.challenge_id);
      setMaskedEmail(result.masked_email);
      setResetCode("");
      setNotice(`驗證碼已寄到 ${result.masked_email}`);
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "無法寄送驗證碼",
      );
    } finally {
      setSubmitting(false);
    }
  }

  async function submitPasswordReset(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!resetChallengeId) {
      setError("請先取得驗證碼");
      return;
    }
    if (!/^\d{6}$/.test(resetCode)) {
      setError("請輸入 6 位數驗證碼");
      return;
    }
    if (form.newPassword.length < 8) {
      setError("會員密碼至少需要 8 個字元");
      return;
    }
    if (!/[A-Za-z]/.test(form.newPassword) || !/\d/.test(form.newPassword)) {
      setError("會員密碼需同時包含英文字母與數字");
      return;
    }
    if (form.newPassword !== form.confirmPassword) {
      setError("兩次輸入的會員密碼不一致");
      return;
    }

    setSubmitting(true);
    setError("");
    try {
      await confirmPasswordReset(
        resetChallengeId,
        resetCode,
        form.newPassword,
      );
      setResetChallengeId("");
      setResetCode("");
      setMaskedEmail("");
      setForm(EMPTY_PASSWORD_FORM);
      setPasswordOpen(false);
      setNotice("會員密碼已透過 Email 驗證完成重設。");
    } catch (requestError) {
      setError(
        requestError instanceof Error ? requestError.message : "重設密碼失敗",
      );
    } finally {
      setSubmitting(false);
    }
  }

  if (loading) {
    return <p className="account-inline-message">正在讀取會員資料…</p>;
  }
  if (!account) {
    return (
      <p className="account-security-error">
        {error || "請重新登入後管理會員帳號。"}
      </p>
    );
  }

  return (
    <div className="account-inline-panel account-security-panel">
      <div className="account-security-heading">
        <div>
          <small>ACCOUNT</small>
          <h3>登入與安全</h3>
        </div>
        <p>管理會員帳號、密碼與登入方式</p>
      </div>
      <div className="account-inline-row account-username-row">
        <span>會員帳號</span>
        <div className="account-password-area">
          <div className="account-username-control">
            <span className="account-username-value">
              {account.username || "尚未設定"}
            </span>
            <button
              type="button"
              className="password-toggle-button"
              aria-expanded={usernameOpen}
              aria-haspopup="dialog"
              onClick={() => {
                setUsernameDraft(account.username || "");
                setUsernameOpen(true);
                setError("");
                setNotice("");
              }}
            >
              <span>{usernameChangeLocked ? "查看更名時間" : "修改帳號"}</span>
            </button>
            <button
              type="button"
              className="password-toggle-button"
              aria-expanded={passwordOpen}
              aria-haspopup="dialog"
              onClick={() => {
                setPasswordOpen(true);
                setError("");
                setNotice("");
              }}
            >
              <img src="/brands/password-key-lavender.png" alt="" className="password-toggle-icon" aria-hidden="true" />
              <span>{account.password_set ? "變更密碼" : "設定密碼"}</span>
            </button>
          </div>

          {usernameOpen ? (
            <div
              className="account-password-modal-backdrop"
              onMouseDown={(event) => {
                if (event.target === event.currentTarget) closeUsernameDialog();
              }}
            >
              <section
                className="account-password-modal"
                role="dialog"
                aria-modal="true"
                aria-labelledby="account-username-modal-title"
              >
                <header className="account-password-modal-header">
                  <span className="account-password-modal-icon" aria-hidden="true">
                    @
                  </span>
                  <div>
                    <small>MEMBER USERNAME</small>
                    <h2 id="account-username-modal-title">修改會員帳號</h2>
                  </div>
                  <button
                    type="button"
                    className="account-password-modal-close"
                    aria-label="關閉帳號修改視窗"
                    onClick={closeUsernameDialog}
                  >
                    ×
                  </button>
                </header>
                <p className="account-password-modal-copy">
                  {usernameChangeLocked
                    ? `你已修改過帳號，${usernameAvailableLabel} 後才能再次更換。`
                    : "帳號名稱修改後，7 天內無法再次變更，請確認後再送出。"}
                </p>
                {error ? <p className="account-security-error" role="alert">{error}</p> : null}
                <form className="account-password-form" onSubmit={submitUsername}>
                  <label>
                    <span>新會員帳號</span>
                    <input
                      value={usernameDraft}
                      onChange={(event) => {
                        setUsernameDraft(event.target.value);
                        setError("");
                      }}
                      autoComplete="username"
                      disabled={usernameChangeLocked || submitting}
                      maxLength={20}
                      placeholder="例如 blink.user"
                    />
                  </label>
                  <small>可使用 4–20 位小寫英文、數字、句點與底線。</small>
                  <button
                    type="submit"
                    className="primary-btn"
                    disabled={usernameChangeLocked || submitting}
                  >
                    {submitting ? "處理中…" : "確認變更"}
                  </button>
                </form>
              </section>
            </div>
          ) : null}

          {passwordOpen ? (
            <div
              className="account-password-modal-backdrop"
              onMouseDown={(event) => {
                if (event.target === event.currentTarget) closePasswordDialog();
              }}
            >
              <section
                className="account-password-modal"
                role="dialog"
                aria-modal="true"
                aria-labelledby="account-password-modal-title"
              >
                <header className="account-password-modal-header">
                  <span className="account-password-modal-icon" aria-hidden="true">
                    <img src="/brands/password-key-lavender.png" alt="" aria-hidden="true" />
                  </span>
                  <div>
                    <small>ACCOUNT SECURITY</small>
                    <h2 id="account-password-modal-title">
                      {account.password_set ? "變更密碼" : "設定密碼"}
                    </h2>
                  </div>
                  <button
                    type="button"
                    className="account-password-modal-close"
                    aria-label="關閉密碼視窗"
                    onClick={closePasswordDialog}
                  >
                    ×
                  </button>
                </header>
                <p className="account-password-modal-copy">
                  {account.password_set
                    ? "先確認是你本人，再設定新的登入密碼。"
                    : "設定一組專屬密碼，之後也能用會員帳號登入。"}
                </p>
                {error ? <p className="account-security-error" role="alert">{error}</p> : null}
                {notice ? <p className="account-security-success">{notice}</p> : null}
                {resetChallengeId ? (
              <form
                className="account-password-form"
                onSubmit={submitPasswordReset}
              >
                <p>驗證碼已寄到 {maskedEmail}</p>
                <label>
                  <span>6 位數驗證碼</span>
                  <input
                    inputMode="numeric"
                    maxLength={6}
                    value={resetCode}
                    onChange={(event) => {
                      setResetCode(event.target.value.replace(/\D/g, ""));
                      setError("");
                    }}
                    autoComplete="one-time-code"
                  />
                </label>
                <label>
                  <span>新密碼</span>
                  <input
                    type="password"
                    value={form.newPassword}
                    onChange={(event) =>
                      updateField("newPassword", event.target.value)
                    }
                    autoComplete="new-password"
                  />
                </label>
                <label>
                  <span>再次輸入新密碼</span>
                  <input
                    type="password"
                    value={form.confirmPassword}
                    onChange={(event) =>
                      updateField("confirmPassword", event.target.value)
                    }
                    autoComplete="new-password"
                  />
                </label>
                <button type="submit" className="primary-btn" disabled={submitting}>
                  {submitting ? "處理中…" : "驗證並重設密碼"}
                </button>
              </form>
            ) : (
            <form className="account-password-form" onSubmit={submitPassword}>
              {account.password_set && !currentPasswordVerified ? (
                <div className="password-verification-step">
                  <label>
                    <span>原密碼</span>
                    <input
                      type="password"
                      value={form.currentPassword}
                      onChange={(event) =>
                        updateField("currentPassword", event.target.value)
                      }
                      autoComplete="current-password"
                    />
                  </label>
                  <button
                    type="button"
                    className="secondary-btn"
                    disabled={submitting}
                    onClick={() => void verifyPassword()}
                  >
                    驗證原密碼
                  </button>
                  <button
                    type="button"
                    className="text-button"
                    disabled={submitting}
                    onClick={() => void requestResetCode()}
                  >
                    忘記原密碼？
                  </button>
                </div>
              ) : (
                <>
                  <label>
                    <span>新密碼</span>
                    <input
                      type="password"
                      value={form.newPassword}
                      onChange={(event) => updateField("newPassword", event.target.value)}
                      autoComplete="new-password"
                    />
                  </label>
                  <label>
                    <span>再次輸入新密碼</span>
                    <input
                      type="password"
                      value={form.confirmPassword}
                      onChange={(event) =>
                        updateField("confirmPassword", event.target.value)
                      }
                      autoComplete="new-password"
                    />
                  </label>
                  <button type="submit" className="primary-btn" disabled={submitting}>
                    {submitting ? "處理中…" : "確認新密碼"}
                  </button>
                </>
              )}
            </form>
            )}
              </section>
            </div>
          ) : null}

        </div>
      </div>

      <div className="account-inline-row account-provider-row">
        <span>帳號綁定</span>
        <div className="account-provider-list">
          {(["google", "line"] as AuthProvider[]).map((provider) => {
            const linked = account.providers.includes(provider);
            const providerContent = (
              <>
                <span className="provider-brand-mark" aria-hidden="true">
                  <img
                    src={provider === "google" ? "/brands/google-g-clean.png" : "/brands/line.png"}
                    alt=""
                    width={36}
                    height={36}
                  />
                </span>
                <span className="provider-name">{providerLabel(provider)}</span>
                <strong className="provider-status">
                  {linked ? "✓ 已綁定" : "立即綁定"}
                </strong>
              </>
            );

            return (
              <button
                key={provider}
                type="button"
                className="provider-link-card"
                onClick={() => handleProviderClick(provider, linked)}
                aria-label={
                  linked
                    ? `管理 ${providerLabel(provider)} 綁定`
                    : `立即綁定 ${providerLabel(provider)}`
                }
              >
                {providerContent}
              </button>
            );
          })}
        </div>
      </div>

      {providerToUnlink ? (
        <div
          className="account-password-modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeProviderDialog();
          }}
        >
          <section
            className="account-password-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="account-provider-modal-title"
          >
            <header className="account-password-modal-header">
              <span className="account-password-modal-icon" aria-hidden="true">
                <img
                  src={
                    providerToUnlink === "google"
                      ? "/brands/google-g-clean.png"
                      : "/brands/line.png"
                  }
                  alt=""
                />
              </span>
              <div>
                <small>ACCOUNT CONNECTION</small>
                <h2 id="account-provider-modal-title">
                  解除 {providerLabel(providerToUnlink)} 綁定？
                </h2>
              </div>
              <button
                type="button"
                className="account-password-modal-close"
                aria-label="關閉解除綁定視窗"
                onClick={closeProviderDialog}
              >
                ×
              </button>
            </header>
            <p className="account-password-modal-copy">
              {unlinkWouldRemoveLastLogin
                ? `目前只有 ${providerLabel(providerToUnlink)} 這一種登入方式。請先設定密碼或綁定另一個平台，再解除綁定。`
                : `解除後將無法再使用 ${providerLabel(providerToUnlink)} 快速登入；你的基本資料與對話記錄不會被刪除。`}
            </p>
            {error ? <p className="account-security-error" role="alert">{error}</p> : null}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr",
                gap: 10,
                marginTop: 18,
              }}
            >
              <button
                type="button"
                className="secondary-btn"
                disabled={submitting}
                onClick={closeProviderDialog}
              >
                取消
              </button>
              <button
                type="button"
                className="primary-btn"
                disabled={unlinkWouldRemoveLastLogin || submitting}
                onClick={() => void confirmProviderUnlink()}
              >
                {submitting ? "處理中…" : "確認解除"}
              </button>
            </div>
          </section>
        </div>
      ) : null}

      {!passwordOpen && !providerToUnlink && !usernameOpen && error ? (
        <p className="account-security-error" role="alert">{error}</p>
      ) : null}
      {!passwordOpen && !providerToUnlink && !usernameOpen && notice ? (
        <p className="account-security-success">{notice}</p>
      ) : null}
    </div>
  );
}
