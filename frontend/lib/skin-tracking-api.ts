import type {
  SkinRecommendationsResponse,
  SkinReportResponse,
  SkinTrackingResponse
} from "@/types/skin-tracking";

import { API_ORIGIN } from "@/lib/api-origin";
import { invalidateQuery, queryCached } from "@/lib/query-cache";

const API_BASE = API_ORIGIN;

function apiFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  return fetch(input, { ...init, credentials: "include" });
}

export class SkinTrackingApiError extends Error {
  constructor(
    readonly status: number,
    message: string
  ) {
    super(message);
    this.name = "SkinTrackingApiError";
  }
}

const REPORT_REQUEST_TIMEOUT_MS = 30_000;

async function reportFetch(
  input: RequestInfo | URL,
  init: RequestInit = {}
): Promise<Response> {
  const controller = new AbortController();
  const timeout = setTimeout(
    () => controller.abort(),
    REPORT_REQUEST_TIMEOUT_MS
  );

  try {
    return await apiFetch(input, { ...init, signal: controller.signal });
  } catch (error) {
    if (controller.signal.aborted) {
      throw new SkinTrackingApiError(
        408,
        "Skin report request timed out"
      );
    }
    throw error;
  } finally {
    clearTimeout(timeout);
  }
}

export async function sendSkinTrackingMessage(
  input: { message: string; sessionId: string },
  token?: string
): Promise<SkinTrackingResponse> {
  const response = await apiFetch(`${API_BASE}/api/skin-tracking/message`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {})
    },
    body: JSON.stringify({
      message: input.message,
      session_id: input.sessionId
    })
  });
  if (!response.ok) {
    throw new SkinTrackingApiError(
      response.status,
      `Skin Tracking request failed: ${response.status}`
    );
  }
  invalidateQuery("report:");
  return (await response.json()) as SkinTrackingResponse;
}

export async function createSkinRecord(input: {
  recordDate: string;
  region: string;
  conditions: string[];
  severity: number;
  notes?: string;
  sessionId?: string;
}): Promise<Record<string, unknown>> {
  const response = await apiFetch(`${API_BASE}/api/skin-records/frontend`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      recordDate: input.recordDate,
      region: input.region,
      conditions: input.conditions,
      severity: input.severity,
      notes: input.notes || "",
      session_id: input.sessionId
    })
  });
  if (!response.ok) {
    throw new SkinTrackingApiError(
      response.status,
      `Skin record request failed: ${response.status}`
    );
  }
  invalidateQuery("report:");
  return (await response.json()) as Record<string, unknown>;
}

async function fetchCurrentSkinReport(
  periodDays = 14,
  token?: string,
  userId?: string
): Promise<SkinReportResponse> {
  const params = new URLSearchParams({ period_days: String(periodDays) });
  if (userId?.trim()) params.set("user_id", userId.trim());
  const response = await reportFetch(
    `${API_BASE}/api/skin-tracking/reports/current?${params.toString()}`,
    {
      method: "GET",
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      cache: "no-store"
    }
  );
  if (!response.ok) {
    throw new SkinTrackingApiError(
      response.status,
      `Skin report request failed: ${response.status}`
    );
  }
  return (await response.json()) as SkinReportResponse;
}

async function fetchSkinReportRecommendations(
  periodDays = 14,
  limit = 3,
  token?: string,
  userId?: string,
  dateRange?: { from: string; to: string }
): Promise<SkinRecommendationsResponse> {
  const params = new URLSearchParams({
    period_days: String(periodDays),
    limit: String(limit)
  });
  if (userId?.trim()) params.set("user_id", userId.trim());
  if (dateRange) {
    params.set("date_from", dateRange.from);
    params.set("date_to", dateRange.to);
  }
  const response = await reportFetch(
    `${API_BASE}/api/skin-tracking/reports/recommendations?${params.toString()}`,
    {
      method: "GET",
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      cache: "no-store"
    }
  );
  if (!response.ok) {
    throw new SkinTrackingApiError(
      response.status,
      `Skin recommendations request failed: ${response.status}`
    );
  }
  return (await response.json()) as SkinRecommendationsResponse;
}

export async function getPartnerSkinReportRecommendations(
  periodDays = 14,
  limit = 3,
  token?: string,
  userId?: string,
  dateRange?: { from: string; to: string }
): Promise<SkinRecommendationsResponse> {
  const params = new URLSearchParams({
    period_days: String(periodDays),
    limit: String(limit)
  });
  if (userId?.trim()) params.set("user_id", userId.trim());
  if (dateRange) {
    params.set("date_from", dateRange.from);
    params.set("date_to", dateRange.to);
  }
  const response = await reportFetch(
    `${API_BASE}/api/skin-tracking/reports/partner-recommendations?${params.toString()}`,
    {
      method: "GET",
      headers: token ? { Authorization: `Bearer ${token}` } : undefined,
      cache: "no-store"
    }
  );
  if (!response.ok) {
    throw new SkinTrackingApiError(
      response.status,
      `Partner skin recommendations request failed: ${response.status}`
    );
  }
  return (await response.json()) as SkinRecommendationsResponse;
}


function reportCacheScope(token?: string): string {
  return token ? token.slice(-12) : "cookie";
}

export function getCurrentSkinReport(
  periodDays = 14,
  token?: string,
  userId?: string,
): Promise<SkinReportResponse> {
  const key = `report:current:${userId?.trim() || "self"}:${periodDays}:${reportCacheScope(token)}`;
  return queryCached(
    key,
    () => fetchCurrentSkinReport(periodDays, token, userId),
    60_000,
  );
}

export function getSkinReportRecommendations(
  periodDays = 14,
  limit = 3,
  token?: string,
  userId?: string,
  dateRange?: { from: string; to: string },
): Promise<SkinRecommendationsResponse> {
  const range = dateRange ? `${dateRange.from}:${dateRange.to}` : "latest";
  const key = `report:recommendations:${userId?.trim() || "self"}:${periodDays}:${limit}:${range}:${reportCacheScope(token)}`;
  return queryCached(
    key,
    () => fetchSkinReportRecommendations(periodDays, limit, token, userId, dateRange),
    60_000,
  );
}
