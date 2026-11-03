import { NextRequest, NextResponse } from "next/server";
import { serverMemberAuthOrigin } from "@/lib/api-origin";

const API_BASE = serverMemberAuthOrigin();

async function proxyMemberAuth(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
) {
  const { path: pathParts } = await context.params;
  const path = pathParts.join("/");
  const target = new URL(`${API_BASE}/api/auth/${path}`);
  target.search = request.nextUrl.search;

  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  const cookie = request.headers.get("cookie");
  const accept = request.headers.get("accept");
  if (contentType) headers.set("content-type", contentType);
  if (cookie) headers.set("cookie", cookie);
  if (accept) headers.set("accept", accept);
  headers.set("x-forwarded-host", request.headers.get("host") ?? "");
  headers.set("x-forwarded-proto", request.nextUrl.protocol.replace(":", ""));

  const upstream = await fetch(target, {
    method: request.method,
    headers,
    body: ["GET", "HEAD"].includes(request.method)
      ? undefined
      : await request.arrayBuffer(),
    cache: "no-store",
    redirect: "manual",
  });

  const responseHeaders = new Headers();
  const upstreamContentType = upstream.headers.get("content-type");
  const location = upstream.headers.get("location");
  const cacheControl = upstream.headers.get("cache-control");
  if (upstreamContentType) responseHeaders.set("content-type", upstreamContentType);
  if (location) responseHeaders.set("location", location);
  if (cacheControl) responseHeaders.set("cache-control", cacheControl);

  // OAuth responses can contain more than one Set-Cookie header.  Collapsing
  // them into a comma-separated value breaks parsing because Expires itself
  // contains commas, so preserve each header independently.
  const cookieHeaders = upstream.headers as Headers & {
    getSetCookie?: () => string[];
  };
  const setCookies = cookieHeaders.getSetCookie?.() ?? [];
  if (setCookies.length > 0) {
    for (const value of setCookies) {
      responseHeaders.append("set-cookie", value);
    }
  } else {
    const setCookie = upstream.headers.get("set-cookie");
    if (setCookie) responseHeaders.append("set-cookie", setCookie);
  }

  return new NextResponse(upstream.body, {
    status: upstream.status,
    headers: responseHeaders,
  });
}

export const dynamic = "force-dynamic";
export const GET = proxyMemberAuth;
export const POST = proxyMemberAuth;
export const PATCH = proxyMemberAuth;
export const DELETE = proxyMemberAuth;
