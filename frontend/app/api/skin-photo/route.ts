import { NextRequest, NextResponse } from "next/server";
import { serverMemberAuthOrigin } from "@/lib/api-origin";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
const service = "http://127.0.0.1:8025";
const visits = new Map<string, number[]>();
const json = (body: unknown, status = 200) => NextResponse.json(body, {
  status, headers: { "Cache-Control": "no-store" },
});

export async function GET() {
  try {
    const response = await fetch(service + "/health", { cache: "no-store", signal: AbortSignal.timeout(3000) });
    if (!response.ok) return json({ ready: false }, 503);
    return json(await response.json());
  } catch { return json({ ready: false }, 503); }
}

export async function POST(request: NextRequest) {
  // The browser sends to its own origin; cookies never reach the model service.
  const origin = request.headers.get("origin");
  let originMatches = false;
  try {
    const url = new URL(origin || "");
    originMatches = ["http:", "https:"].includes(url.protocol) && url.host === (request.headers.get("host") || "").toLowerCase();
  } catch { /* Missing or malformed Origin is rejected. */ }
  if (!originMatches) return json({ error: "origin_not_allowed" }, 403);
  let userId = "";
  try {
    const headers = new Headers({ Accept: "application/json" });
    headers.set("cookie", request.headers.get("cookie") || "");
    headers.set("x-forwarded-host", request.headers.get("host") || "");
    headers.set("x-forwarded-proto", request.nextUrl.protocol.replace(":", ""));
    const auth = await fetch(serverMemberAuthOrigin() + "/api/auth/session", {
      headers, cache: "no-store", signal: AbortSignal.timeout(10000),
    });
    if (auth.status === 401 || auth.status === 403) return json({ error: "login_required" }, 401);
    if (!auth.ok) return json({ error: "auth_unavailable" }, 503);
    const payload = await auth.json();
    const account = payload.account || payload;
    if (typeof account.user_id !== "string" || !account.user_id.trim()) return json({ error: "login_required" }, 401);
    userId = account.user_id;
  } catch { return json({ error: "auth_unavailable" }, 503); }

  const now = Date.now();
  const recent = (visits.get(userId) || []).filter(value => now - value < 60000);
  if (recent.length >= 4) return json({ error: "rate_limited" }, 429);
  visits.set(userId, [...recent, now]);
  if (visits.size > 1000) {
    for (const [key, values] of visits) if (!values.some(value => now - value < 60000)) visits.delete(key);
  }
  const max = 5 * 1024 * 1024;
  if (Number(request.headers.get("content-length") || 0) > max) return json({ error: "image_too_large" }, 413);
  if (!request.body) return json({ error: "missing_image" }, 400);
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      length += part.value.length;
      if (length > max) { await reader.cancel(); return json({ error: "image_too_large" }, 413); }
      chunks.push(part.value);
    }
    const bytes = new Uint8Array(length);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
    const body = JSON.parse(new TextDecoder().decode(bytes));
    if (!body || typeof body !== "object" || (!body.sample && typeof body.image_base64 !== "string")) {
      return json({ error: "missing_image" }, 400);
    }
    // No body/image logging or storage; the local service deletes its temporary file.
    const response = await fetch(service + "/analyze", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sample: body.sample === true, image_base64: body.image_base64, note: typeof body.note === "string" ? body.note.slice(0, 500) : "" }),
      cache: "no-store", signal: AbortSignal.timeout(90000),
    });
    return json(await response.json(), response.status);
  } catch (error) {
    return json({ error: error instanceof SyntaxError ? "invalid_request" : "inference_unavailable" }, error instanceof SyntaxError ? 400 : 503);
  }
}
