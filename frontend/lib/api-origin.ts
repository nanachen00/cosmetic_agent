const trimTrailingSlash = (value: string): string => value.replace(/\/+$/, "");

const PUBLIC_API_ORIGIN = "https://api.blinkai.tw";
const LOCAL_API_ORIGIN = "";

export const isLoopbackHostname = (hostname: string): boolean =>
  hostname === "localhost" || hostname === "127.0.0.1" || hostname === "::1";

const isUsablePublicOrigin = (value: string): boolean => {
  if (!value || /\[(?:sensitive|redacted)\]/i.test(value)) {
    return false;
  }

  try {
    const url = new URL(value);
    return (
      (url.protocol === "https:" || url.protocol === "http:") &&
      !isLoopbackHostname(url.hostname)
    );
  } catch {
    return false;
  }
};

const browserIsLocal =
  typeof window !== "undefined" && isLoopbackHostname(window.location.hostname);

const configuredPublicOrigin = trimTrailingSlash(
  process.env.NEXT_PUBLIC_API_BASE_URL || "",
);
const configuredLocalOrigin = trimTrailingSlash(
  process.env.NEXT_PUBLIC_LOCAL_API_BASE_URL || LOCAL_API_ORIGIN,
);

/** Single browser-facing API origin for login, OAuth and public API calls. */
export const API_ORIGIN = trimTrailingSlash(
  browserIsLocal
    ? configuredLocalOrigin
    : isUsablePublicOrigin(configuredPublicOrigin)
      ? configuredPublicOrigin
      : PUBLIC_API_ORIGIN,
);

/** Server-side origin used only by the same-origin /member-auth proxy. */
export function serverMemberAuthOrigin(): string {
  const developmentServerOrigin = "http://127.0.0.1:8010";
  if (process.env.NODE_ENV !== "production") {
    return trimTrailingSlash(
      process.env.DEV_MEMBER_AUTH_API_BASE_URL || developmentServerOrigin,
    );
  }
  return trimTrailingSlash(
    process.env.MEMBER_AUTH_API_BASE_URL || API_ORIGIN,
  );
}

/**
 * Browser-facing member-auth base.
 *
 * Local development must keep every cookie-bearing auth request on the
 * frontend origin.  Mixing localhost and 127.0.0.1 makes the browser keep two
 * different cookie jars, which causes OAuth to appear successful and then be
 * treated as logged out on the next page.
 */
export function browserMemberAuthBase(): string {
  if (
    typeof window !== "undefined" &&
    isLoopbackHostname(window.location.hostname)
  ) {
    return "/member-auth";
  }
  return `${API_ORIGIN}/api/auth`;
}

export function browserMemberAuthUrl(path: string): string {
  const normalizedPath = path.startsWith("/") ? path : `/${path}`;
  return `${browserMemberAuthBase()}${normalizedPath}`;
}
