import {
  API_ORIGIN,
  browserMemberAuthBase,
  browserMemberAuthUrl,
} from "@/lib/api-origin";

const DIRECT_API_BASE = API_ORIGIN;

function accountApiBase(): string {
  return browserMemberAuthBase();
}

export type AuthProvider = "google" | "line";

export type AccountSecurityState = {
  user_id: string;
  username: string;
  display_name: string | null;
  email: string | null;
  email_verified: boolean;
  password_set: boolean;
  providers: AuthProvider[];
  auth_method: string;
  username_changed_at: string | null;
  username_change_available_at: string | null;
};

export type PasswordResetRequestResult = {
  ok: true;
  challenge_id: string;
  masked_email: string;
};

export type PublicPasswordRecoveryRequestResult = {
  ok: true;
  request_id: string;
  expires_in_seconds: number;
};

export type PublicPasswordRecoveryConfirmResult = {
  ok: true;
  user_id: string;
};

async function readError(response: Response): Promise<string> {
  try {
    const data = (await response.json()) as { detail?: string };
    return "操作失敗，請稍後再試";
  } catch {
    return "操作失敗，請稍後再試";
  }
}

async function accountRequest<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${accountApiBase()}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...init.headers,
    },
  });

  if (!response.ok) {
    throw new Error(await readError(response));
  }
  return (await response.json()) as T;
}

async function publicAuthRequest<T>(
  path: string,
  body: Record<string, string>,
): Promise<T> {
  const response = await fetch(`${DIRECT_API_BASE}${path}`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    throw new Error(await readError(response));
  }
  return (await response.json()) as T;
}

export function requestPublicPasswordRecovery(
  email: string,
): Promise<PublicPasswordRecoveryRequestResult> {
  return publicAuthRequest<PublicPasswordRecoveryRequestResult>(
    "/api/auth/password/recovery/request",
    { email },
  );
}

export function confirmPublicPasswordRecovery(
  requestId: string,
  code: string,
  newPassword: string,
): Promise<PublicPasswordRecoveryConfirmResult> {
  return publicAuthRequest<PublicPasswordRecoveryConfirmResult>(
    "/api/auth/password/recovery/confirm",
    {
      request_id: requestId,
      code,
      new_password: newPassword,
    },
  );
}

export function getAccountSecurity(): Promise<AccountSecurityState> {
  return accountRequest<AccountSecurityState>("/session");
}

export function changeAccountUsername(
  username: string,
): Promise<AccountSecurityState> {
  return accountRequest<AccountSecurityState>("/username", {
    method: "PATCH",
    body: JSON.stringify({ username }),
  });
}

export function unlinkAccountProvider(
  provider: AuthProvider,
): Promise<AccountSecurityState> {
  return accountRequest<AccountSecurityState>(`/providers/${provider}`, {
    method: "DELETE",
  });
}

export function setInitialPassword(newPassword: string): Promise<void> {
  return accountRequest<void>("/password/set", {
    method: "POST",
    body: JSON.stringify({ new_password: newPassword }),
  });
}

export function changeAccountPassword(
  currentPassword: string,
  newPassword: string,
): Promise<void> {
  return accountRequest<void>("/password/change", {
    method: "POST",
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
  });
}

export function verifyCurrentPassword(
  currentPassword: string,
): Promise<void> {
  return accountRequest<void>("/password/verify", {
    method: "POST",
    body: JSON.stringify({ current_password: currentPassword }),
  });
}

export function getProviderLinkUrl(provider: AuthProvider): string {
  return `${browserMemberAuthUrl(`/${provider}/login`)}?mode=link`;
}

export function requestPasswordResetCode(): Promise<PasswordResetRequestResult> {
  return accountRequest<PasswordResetRequestResult>("/password/reset/request", {
    method: "POST",
  });
}

export function confirmPasswordReset(
  challengeId: string,
  code: string,
  newPassword: string,
): Promise<void> {
  return accountRequest<void>("/password/reset/confirm", {
    method: "POST",
    body: JSON.stringify({
      challenge_id: challengeId,
      code,
      new_password: newPassword,
    }),
  });
}
