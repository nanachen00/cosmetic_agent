"use client";

import { browserMemberAuthUrl } from "@/lib/api-origin";

export type AuthIdentity = {
  userId: string;
  username: string;
  displayName: string;
  email: string;
  provider: string;
};

const AUTH_CACHE_TTL_MS = 30_000;
const AUTH_EVENT_KEY = "blink_auth_event";
const AUTH_CHANNEL = "blink-auth";

function authFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  const timeoutSignal = AbortSignal.timeout(15_000);
  const signal = init.signal
    ? AbortSignal.any([init.signal, timeoutSignal])
    : timeoutSignal;
  return fetch(input, { ...init, signal });
}

const LEGACY_IDENTITY_KEYS = [
  "blink_user_id",
  "blink_username",
  "blink_display_name",
  "blink_email",
  "blink_auth_provider",
];

const SENSITIVE_STORAGE_PREFIXES = [
  "blink_chat_history:",
  "blink_chat_pending_v1:",
  "blink_chat_assistant_route:",
  "blink-checkin-status:",
  "blink-customer-quiz-draft:",
];

let cachedIdentity: AuthIdentity | null | undefined;
let cachedUntil = 0;
let identityRequest: Promise<AuthIdentity | null> | null = null;

function removeMatchingKeys(storage: Storage, prefixes: string[]): void {
  for (let index = storage.length - 1; index >= 0; index -= 1) {
    const key = storage.key(index);
    if (key && prefixes.some((prefix) => key.startsWith(prefix))) {
      storage.removeItem(key);
    }
  }
}

export function clearLegacySensitiveStorage(): void {
  if (typeof window === "undefined") return;

  try {
    for (const key of LEGACY_IDENTITY_KEYS) {
      window.localStorage.removeItem(key);
    }
    removeMatchingKeys(window.localStorage, SENSITIVE_STORAGE_PREFIXES);
  } catch {
    // Storage may be unavailable in private browsing or restricted contexts.
  }
}

export function clearSensitiveSessionStorage(): void {
  if (typeof window === "undefined") return;

  try {
    removeMatchingKeys(window.sessionStorage, [
...LEGACY_IDENTITY_KEYS,
      ...SENSITIVE_STORAGE_PREFIXES,
      "blink_guest_user_id",
      "blink-checkin-user-id",
    ]);
  } catch {
    // Storage may be unavailable in private browsing or restricted contexts.
  }
}

function normalizeIdentity(payload: unknown): AuthIdentity | null {
  if (!payload || typeof payload !== "object") return null;

  const root = payload as Record<string, unknown>;
  const candidate =
    root.account && typeof root.account === "object"
      ? (root.account as Record<string, unknown>)
      : root;
  const userId =
    typeof candidate.user_id === "string" ? candidate.user_id.trim() : "";

  if (!userId) return null;

  const readString = (key: string): string =>
    typeof candidate[key] === "string" ? (candidate[key] as string).trim() : "";

  return {
    userId,
    username: readString("username"),
    displayName: readString("display_name") || readString("username"),
    email: readString("email"),
    provider: readString("provider"),
  };
}

export async function getCurrentAuthIdentity(options?: {
  force?: boolean;
}): Promise<AuthIdentity | null> {
  if (typeof window === "undefined") return null;

  clearLegacySensitiveStorage();

  const now = Date.now();
  if (!options?.force && cachedIdentity !== undefined && now < cachedUntil) {
    return cachedIdentity;
  }
  if (identityRequest) return identityRequest;

  identityRequest = (async () => {
    const response = await authFetch(
      browserMemberAuthUrl("/session"),
      {
        credentials: "include",
        cache: "no-store",
        headers: { Accept: "application/json" },
      },
    );

    if (response.status === 401 || response.status === 403) {
      cachedIdentity = null;
      cachedUntil = Date.now() + 5_000;
      return null;
    }
    if (!response.ok) {
      throw new Error("Unable to verify the current session");
    }

    cachedIdentity = normalizeIdentity(await response.json());
    cachedUntil = Date.now() + AUTH_CACHE_TTL_MS;
    return cachedIdentity;
  })();

  try {
    return await identityRequest;
  } finally {
    identityRequest = null;
  }
}

export function invalidateAuthIdentity(): void {
  cachedIdentity = undefined;
  cachedUntil = 0;
  identityRequest = null;
  clearLegacySensitiveStorage();
}

const GUEST_USER_ID_PATTERN = /^guest-[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
let ephemeralGuestUserId: string | null = null;
let guestIdentityRequest: Promise<string> | null = null;

function createGuestUserId(): string {
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `guest-${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export function getOrCreateGuestUserId(): string {
  if (typeof window === "undefined") return "anonymous";

  const key = "blink_guest_user_id";
  try {
    const stored = window.sessionStorage.getItem(key)?.trim();
    if (stored && GUEST_USER_ID_PATTERN.test(stored)) return stored.toLowerCase();

    const guestId =
      typeof window.crypto.randomUUID === "function"
        ? `guest-${window.crypto.randomUUID()}`
        : createGuestUserId();
    window.sessionStorage.setItem(key, guestId);
    return guestId;
  } catch {
    ephemeralGuestUserId ||= createGuestUserId();
    return ephemeralGuestUserId;
  }
}

export async function resolveCurrentUserId(): Promise<string> {
  const identity = await getCurrentAuthIdentity();
  if (identity?.userId) return identity.userId;
  if (guestIdentityRequest) return guestIdentityRequest;

  guestIdentityRequest = (async () => {
    const response = await authFetch(browserMemberAuthUrl("/guest-session"), {
      method: "POST",
      credentials: "include",
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) {
      throw new Error("Unable to create a protected guest session");
    }
    const payload = (await response.json()) as { user_id?: unknown };
    const userId =
      typeof payload.user_id === "string" ? payload.user_id.trim().toLowerCase() : "";
    if (!GUEST_USER_ID_PATTERN.test(userId)) {
      throw new Error("The guest session response is invalid");
    }
    return userId;
  })();

  try {
    return await guestIdentityRequest;
  } finally {
    guestIdentityRequest = null;
  }
}

function publishLogout(): void {
  if (typeof window === "undefined") return;

  try {
    window.localStorage.setItem(AUTH_EVENT_KEY, String(Date.now()));
  } catch {
    // BroadcastChannel remains available in most modern browsers.
  }

  if (typeof BroadcastChannel !== "undefined") {
    const channel = new BroadcastChannel(AUTH_CHANNEL);
    channel.postMessage({ type: "logout" });
    channel.close();
  }
}

export async function logoutCurrentSession(): Promise<void> {
  const response = await authFetch(browserMemberAuthUrl("/logout"), {
    method: "POST",
    credentials: "include",
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  if (!response.ok && response.status !== 401) {
    throw new Error("Logout failed");
  }
  invalidateAuthIdentity();
  clearSensitiveSessionStorage();
  publishLogout();
}

export function subscribeToLogout(onLogout: () => void): () => void {
  if (typeof window === "undefined") return () => undefined;

  const handleStorage = (event: StorageEvent) => {
    if (event.key === AUTH_EVENT_KEY) onLogout();
  };
  const handleMessage = () => onLogout();

  window.addEventListener("storage", handleStorage);
  const channel =
    typeof BroadcastChannel !== "undefined"
      ? new BroadcastChannel(AUTH_CHANNEL)
      : null;
  channel?.addEventListener("message", handleMessage);

  return () => {
    window.removeEventListener("storage", handleStorage);
    channel?.removeEventListener("message", handleMessage);
    channel?.close();
  };
}
