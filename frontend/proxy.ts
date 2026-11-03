import {
  NextRequest,
  NextResponse,
} from "next/server";

function buildContentSecurityPolicy(nonce: string): string {
  const isDevelopment = process.env.NODE_ENV === "development";
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${
      isDevelopment ? " 'unsafe-eval'" : ""
    }`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' blob: data: https:",
    "media-src 'self' blob:",
    "font-src 'self' data:",
    "connect-src 'self' https://api.blinkai.tw https://api.bigdatacloud.net https://*.sentry.io",
    "frame-src 'self' https://accounts.google.com https://access.line.me",
    "worker-src 'self' blob:",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'self'",
    "upgrade-insecure-requests",
  ].join("; ");
}

function applySecurityHeaders(
  response: NextResponse,
  contentSecurityPolicy: string,
  allowCamera = false,
): NextResponse {
  response.headers.set("Content-Security-Policy", contentSecurityPolicy);
  response.headers.set("X-Content-Type-Options", "nosniff");
  response.headers.set("X-Permitted-Cross-Domain-Policies", "none");
  response.headers.set("X-XSS-Protection", "0");
  response.headers.set("X-Frame-Options", "SAMEORIGIN");
  response.headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  response.headers.set(
    "Permissions-Policy",
    `browsing-topics=(), geolocation=(self), microphone=(), camera=${allowCamera ? "(self)" : "()"}, payment=(), usb=()`,
  );
  response.headers.set("Cross-Origin-Opener-Policy", "same-origin-allow-popups");
  if (process.env.NODE_ENV === "production") {
    response.headers.set(
      "Strict-Transport-Security",
      "max-age=63072000; includeSubDomains; preload",
    );
  }
  return response;
}

export function proxy(request: NextRequest) {
  const nonce = btoa(crypto.randomUUID());
  const contentSecurityPolicy = buildContentSecurityPolicy(nonce);
  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("Content-Security-Policy", contentSecurityPolicy);

  const isCustomerQuizRedirect =
    request.nextUrl.pathname === "/chat" &&
    request.nextUrl.searchParams.get("flow") === "customer_quiz";

  if (isCustomerQuizRedirect) {
    const destination = request.nextUrl.clone();
    const mode = destination.searchParams.get("mode");
    destination.pathname = "/personality-quiz";
    destination.search = "";
    if (mode === "retake") {
      destination.searchParams.set("mode", "retake");
    }
    return applySecurityHeaders(
      NextResponse.redirect(destination),
      contentSecurityPolicy,
    );
  }

  return applySecurityHeaders(
    NextResponse.next({
      request: { headers: requestHeaders },
    }),
    contentSecurityPolicy,
    request.nextUrl.pathname === "/skin-photo",
  );
}

export const config = {
  matcher: [
    "/((?!api|_next/static|_next/image|favicon.ico|sitemap.xml|robots.txt|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)",
  ],
};
