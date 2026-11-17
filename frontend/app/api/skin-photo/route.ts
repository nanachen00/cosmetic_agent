import { NextRequest, NextResponse } from "next/server";
import { serverMemberAuthOrigin } from "@/lib/api-origin";
import { PhotoRequestError, PhotoRequestGate, photoModelOrigin, readPhotoJson } from "@/lib/photo-request-limits";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
const configuredService = process.env.PHOTO_MODEL_BASE_URL || "http://127.0.0.1:8025";
const service = photoModelOrigin(configuredService);
const gate = new PhotoRequestGate();
const healthGate = new PhotoRequestGate(2);
const json = (body: unknown, status = 200) => NextResponse.json(body, {
  status, headers: { "Cache-Control": "no-store" },
});

const object = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const text = (value: unknown, max: number, empty = false): value is string =>
  typeof value === "string" && (empty || value.trim().length > 0) && Array.from(value).length <= max;

async function upstreamJson(response: Response, signal: AbortSignal, maxBytes: number, timeoutMs: number) {
  try {
    if (!/^application\/(?:[a-z0-9.+-]*\+)?json(?:\s*;|$)/i.test(response.headers.get("content-type") || "") || !response.body) {
      throw new Error("invalid_upstream");
    }
    const payload = await readPhotoJson(response.body, { signal, maxBytes, timeoutMs });
    if (!object(payload)) throw new Error("invalid_upstream");
    return payload;
  } catch {
    // Malformed/oversized upstream bodies are server failures, not upload errors.
    throw new Error("invalid_upstream");
  } finally { void response.body?.cancel().catch(() => {}); }
}

function publicReport(payload: Record<string, unknown>, sample: boolean) {
  const status = payload.status;
  if (typeof status !== "string" || !["needs_confirmation", "needs_retake", "invalid_model_output"].includes(status)) throw new Error("invalid_upstream");
  const result: Record<string, unknown> = {
    status, source: "experimental_vlm", input_source: sample ? "public_sample" : "user_photo",
    requires_user_confirmation: true, eligible_for_auto_save: false, severity: null,
  };
  if (typeof payload.inference_seconds === "number" && Number.isFinite(payload.inference_seconds) && payload.inference_seconds >= 0) {
    result.inference_seconds = payload.inference_seconds;
  }
  // Invalid model text/error details are internal; retain the explicit product state.
  if (status === "invalid_model_output") return result;
  const observation = payload.observation;
  if (!object(observation) || typeof observation.image_scope !== "string" || !["face", "skin_closeup", "non_skin", "uncertain"].includes(observation.image_scope) ||
      !Array.isArray(observation.observations) || observation.observations.length > 12 ||
      !Array.isArray(observation.limitations) || observation.limitations.length > 8 ||
      !observation.limitations.every(value => text(value, 240)) || !text(observation.follow_up_question, 240, true)) throw new Error("invalid_upstream");
  const skin = observation.image_scope === "face" || observation.image_scope === "skin_closeup";
  if ((skin && (status !== "needs_confirmation" || observation.limitations.length === 0)) ||
      (!skin && (status !== "needs_retake" || observation.observations.length !== 0))) throw new Error("invalid_upstream");
  const findings = observation.observations.map(value => {
    if (!object(value) || typeof value.tag !== "string" || !["acne_like_bumps", "redness", "dark_marks", "flaking"].includes(value.tag) ||
        typeof value.region !== "string" || !["forehead", "cheek", "nose", "chin", "eye_area", "unknown"].includes(value.region) || !text(value.evidence, 240)) throw new Error("invalid_upstream");
    return { tag: value.tag, region: value.region, evidence: value.evidence };
  });
  result.observation = { image_scope: observation.image_scope, observations: findings,
    limitations: observation.limitations, follow_up_question: observation.follow_up_question };
  const quality = payload.quality;
  if (object(quality)) {
    const safeQuality: Record<string, unknown> = {};
    for (const key of ["width", "height"]) if (Number.isSafeInteger(quality[key]) && (quality[key] as number) > 0 && (quality[key] as number) <= 25_000_000) safeQuality[key] = quality[key];
    if (typeof quality.brightness === "number" && Number.isFinite(quality.brightness) && quality.brightness >= 0 && quality.brightness <= 255) safeQuality.brightness = quality.brightness;
    if (Array.isArray(quality.reasons)) safeQuality.reasons = quality.reasons.filter(value => typeof value === "string" && ["too_small", "too_dark", "too_bright", "low_contrast"].includes(value));
    result.quality = safeQuality;
  }
  return result;
}

const upstreamErrors: Record<number, readonly string[]> = {
  400: ["invalid_image", "missing_image"], 413: ["image_too_large"], 429: ["busy"],
  503: ["model_starting", "inference_unavailable", "sample_unavailable"],
};

export async function GET(request?: NextRequest) {
  if (!service) return json({ ready: false, error: "inference_unavailable" }, 503);
  const release = healthGate.enter();
  if (!release) return json({ ready: false, error: "inference_unavailable" }, 503);
  try {
    const signal = AbortSignal.any([AbortSignal.timeout(3000), ...(request ? [request.signal] : [])]);
    const response = await fetch(service + "/health", { cache: "no-store", redirect: "error", signal });
    const payload = await upstreamJson(response, signal, 4096, 3000);
    if (!response.ok || typeof payload.ready !== "boolean" || (payload.failed !== undefined && typeof payload.failed !== "boolean") ||
        (payload.busy !== undefined && typeof payload.busy !== "boolean")) throw new Error("invalid_upstream");
    const ready = payload.ready && payload.failed !== true;
    return json({ ready, ...(typeof payload.busy === "boolean" ? { busy: payload.busy } : {}),
      ...(!ready ? { error: payload.failed === true ? "inference_unavailable" : "model_starting" } : {}) }, ready ? 200 : 503);
  } catch { return json({ ready: false, error: "inference_unavailable" }, 503); }
  finally { release(); }
}

export async function POST(request: NextRequest) {
  // The browser sends to its own origin; cookies never reach the model service.
  const origin = request.headers.get("origin");
  let originMatches = false;
  try {
    const url = new URL(origin || "");
    originMatches = url.origin === origin && url.protocol === request.nextUrl.protocol &&
      ["http:", "https:"].includes(url.protocol) && url.host === (request.headers.get("host") || "").toLowerCase();
  } catch { /* Missing or malformed Origin is rejected. */ }
  if (!originMatches) {
    void request.body?.cancel().catch(() => {});
    return json({ error: "origin_not_allowed" }, 403);
  }
  if (request.signal.aborted) {
    void request.body?.cancel().catch(() => {});
    return json({ error: "request_aborted" }, 400);
  }
  const release = gate.enter();
  if (!release) {
    void request.body?.cancel().catch(() => {});
    return json({ error: "busy" }, 429);
  }
  try {
    let userId = "";
    try {
      const headers = new Headers({ Accept: "application/json" });
      headers.set("cookie", request.headers.get("cookie") || "");
      headers.set("x-forwarded-host", request.headers.get("host") || "");
      headers.set("x-forwarded-proto", request.nextUrl.protocol.replace(":", ""));
      const signal = AbortSignal.any([AbortSignal.timeout(10000), request.signal]);
      const auth = await fetch(serverMemberAuthOrigin() + "/api/auth/session", {
        headers, cache: "no-store", redirect: "error", signal,
      });
      if (auth.status === 401 || auth.status === 403) { void auth.body?.cancel().catch(() => {}); return json({ error: "login_required" }, 401); }
      if (!auth.ok) { void auth.body?.cancel().catch(() => {}); return json({ error: "auth_unavailable" }, 503); }
      const payload = await upstreamJson(auth, signal, 16384, 10000);
      const account = payload.account || payload;
      if (!object(account) || typeof account.user_id !== "string" || !account.user_id.trim() || account.user_id.length > 128) return json({ error: "login_required" }, 401);
      userId = account.user_id;
    } catch {
      if (request.signal.aborted) throw new PhotoRequestError(400, "request_aborted");
      return json({ error: "auth_unavailable" }, 503);
    }

    if (!service) return json({ error: "inference_unavailable" }, 503);
    gate.takeUserQuota(userId);
    const max = 5 * 1024 * 1024;
    const length = request.headers.get("content-length");
    if (length !== null && (!/^\d+$/.test(length) || !Number.isSafeInteger(Number(length)))) return json({ error: "invalid_request" }, 400);
    if (Number(length || 0) > max) return json({ error: "image_too_large" }, 413);
    if (!request.body) return json({ error: "missing_image" }, 400);
    const decoded = await readPhotoJson(request.body, { maxBytes: max, signal: request.signal });
    if (!decoded || typeof decoded !== "object" || Array.isArray(decoded)) return json({ error: "missing_image" }, 400);
    const body = decoded as Record<string, unknown>;
    if (body.sample !== true && typeof body.image_base64 !== "string") {
      return json({ error: "missing_image" }, 400);
    }
    const sample = body.sample === true;
    if (!sample) {
      const encoded = body.image_base64 as string;
      if (!encoded.length) return json({ error: "missing_image" }, 400);
      if (encoded.length > 4 * 1024 * 1024) return json({ error: "image_too_large" }, 413);
      if (encoded.length % 4 !== 0 || !/^[A-Za-z0-9+/]+={0,2}$/.test(encoded)) return json({ error: "invalid_image" }, 400);
      const bytes = Buffer.from(encoded, "base64");
      if (!bytes.length || bytes.toString("base64") !== encoded) return json({ error: "invalid_image" }, 400);
    }
    // No body/image logging or storage; the GPU service decodes uploaded bytes in memory.
    const signal = AbortSignal.any([AbortSignal.timeout(90000), request.signal]);
    const response = await fetch(service + "/analyze", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sample, ...(!sample ? { image_base64: body.image_base64 } : {}), note: typeof body.note === "string" ? body.note.slice(0, 500) : "" }),
      cache: "no-store", redirect: "error", signal,
    });
    const payload = await upstreamJson(response, signal, 65536, 90000);
    if (!response.ok) {
      if (typeof payload.error === "string" && upstreamErrors[response.status]?.includes(payload.error)) return json({ error: payload.error }, response.status);
      return json({ error: "inference_unavailable" }, 503);
    }
    if (response.status !== 200) return json({ error: "inference_unavailable" }, 503);
    return json(publicReport(payload, sample));
  } catch (error) {
    if (request.signal.aborted) return json({ error: "request_aborted" }, 400);
    if (error instanceof PhotoRequestError) return json({ error: error.code }, error.status);
    return json({ error: "inference_unavailable" }, 503);
  } finally {
    void request.body?.cancel().catch(() => {});
    release();
  }
}
