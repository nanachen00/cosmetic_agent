import { API_ORIGIN } from "@/lib/api-origin";

export type PreferredTexture = "light" | "rich";

export interface RecommendationPreferences {
  budget_max: number | null;
  preferred_texture: PreferredTexture | null;
  preferred_brands: string[];
  avoided_brands: string[];
  updated_at: string | null;
}

export type RecommendationPreferencesPatch = Omit<
  RecommendationPreferences,
  "updated_at"
>;

const PREFERENCES_ENDPOINT = `${API_ORIGIN}/api/user/preferences`;
const REQUEST_TIMEOUT_MS = 10_000;

function safePreferences(value: unknown): RecommendationPreferences {
  const record = value && typeof value === "object"
    ? value as Record<string, unknown>
    : {};
  const budget = typeof record.budget_max === "number" && Number.isFinite(record.budget_max)
    ? record.budget_max
    : null;
  const texture = record.preferred_texture === "light" || record.preferred_texture === "rich"
    ? record.preferred_texture
    : null;
  const list = (input: unknown) => Array.isArray(input)
    ? input.filter((item): item is string => typeof item === "string" && item.trim().length > 0)
      .map((item) => item.trim())
    : [];
  return {
    budget_max: budget,
    preferred_texture: texture,
    preferred_brands: list(record.preferred_brands).slice(0, 10),
    avoided_brands: list(record.avoided_brands).slice(0, 10),
    updated_at: typeof record.updated_at === "string" ? record.updated_at : null,
  };
}

async function requestPreferences(
  init: RequestInit,
): Promise<RecommendationPreferences> {
  let response: Response;
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body) headers.set("Content-Type", "application/json");
  try {
    response = await fetch(PREFERENCES_ENDPOINT, {
      ...init,
      credentials: "include",
      cache: "no-store",
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      headers,
    });
  } catch {
    throw new Error("目前無法連線，請稍後再試。");
  }

  if (!response.ok) {
    if (response.status === 401 || response.status === 403) {
      throw new Error("登入狀態已失效，請重新登入。");
    }
    if (response.status === 422) {
      throw new Error("偏好內容格式不正確，請檢查後再儲存。");
    }
    throw new Error("目前無法儲存推薦偏好，請稍後再試。");
  }

  let payload: unknown;
  try {
    payload = await response.json();
  } catch {
    throw new Error("伺服器回應格式不正確，請稍後再試。");
  }
  const record = payload && typeof payload === "object"
    ? payload as Record<string, unknown>
    : {};
  if (!record.preferences || typeof record.preferences !== "object" || Array.isArray(record.preferences)) {
    throw new Error("伺服器回應格式不正確，請稍後再試。");
  }
  return safePreferences(record.preferences);
}

export function getRecommendationPreferences(): Promise<RecommendationPreferences> {
  return requestPreferences({ method: "GET" });
}

export function saveRecommendationPreferences(
  preferences: RecommendationPreferencesPatch,
): Promise<RecommendationPreferences> {
  return requestPreferences({
    method: "PATCH",
    body: JSON.stringify(preferences),
  });
}

export function resetRecommendationPreferences(): Promise<RecommendationPreferences> {
  return requestPreferences({ method: "DELETE" });
}
