import {
  getCurrentAuthIdentity,
  getOrCreateGuestUserId,
} from "@/lib/auth-client";
import { API_ORIGIN } from "@/lib/api-origin";
import { invalidateQuery, queryCached } from "@/lib/query-cache";


/* =========================================================
   API
========================================================= */

const API_BASE_URL = API_ORIGIN;
const API_TIMEOUT_MS = 15_000;

function apiFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  const timeoutSignal = AbortSignal.timeout(API_TIMEOUT_MS);
  const signal = init.signal
    ? AbortSignal.any([init.signal, timeoutSignal])
    : timeoutSignal;
  return fetch(input, { ...init, credentials: "include", signal });
}


/* =========================================================
   User ID Keys
========================================================= */



/* =========================================================
   Types
========================================================= */

export type CheckinEntry = {
  day_number: number;

  date:
    | string
    | null;

  checked_in_at:
    | string
    | null;

  points_earned: number;
};


export type CheckinStatus = {
  ok: boolean;

  user_id: string;

  timezone: string;

  today: string;

  points: number;

  total_checkins: number;

  current_cycle: number;

  completed_days:
    number[];

  completed_count:
    number;

  current_cycle_checkins:
    CheckinEntry[];

  today_checked_in:
    boolean;

  can_check_in:
    boolean;

  cycle_completed:
    boolean;

  next_day:
    | number
    | null;

  beauty_unlocked:
    boolean;

  doctor_unlocked:
    boolean;

  beauty_remaining:
    number;

  doctor_remaining:
    number;

  last_checkin_at:
    | string
    | null;
};


export type DailyCheckinResponse = {
  ok: boolean;

  already: boolean;

  message: string;

  checkin:
    | {
        user_id:
          string;

        date:
          string;

        created_at:
          string;

        checked_in_at:
          string;

        timezone:
          string;

        points_earned:
          number;

        cycle_number:
          number;

        day_number:
          number;
      }
    | null;

  status:
    CheckinStatus;
};


export type CheckinHistoryEntry = {
  cycle_number:
    number;

  day_number:
    number;

  date:
    | string
    | null;

  checked_in_at:
    | string
    | null;

  timezone:
    string;

  points_earned:
    number;
};


export type CheckinHistoryResponse = {
  ok:
    boolean;

  user_id:
    string;

  count:
    number;

  history:
    CheckinHistoryEntry[];
};


export type DailyContent = {
  id: string;
  content_type:
    | "beauty_tip"
    | "dermatologist_faq";
  title: string;
  summary: string;
  source_name: string | null;
  source_url: string | null;
  source_published_at: string | null;
  published_at: string | null;
};


export type DailyContentsResponse = {
  ok: boolean;
  date: string;
  contents: {
    beauty_tip: DailyContent;
    dermatologist_faq: DailyContent;
  };
};



export async function getOrCreateCheckinUserId(): Promise<string> {
  if (
    typeof window ===
    "undefined"
  ) {
    return "anonymous";
  }

  const identity = await getCurrentAuthIdentity();
  return identity?.userId || getOrCreateGuestUserId();

}


/* =========================================================
   Error Helper
========================================================= */

async function getErrorMessage(
  response: Response,
  fallback: string,
): Promise<string> {
  try {
    const body =
      await response.json();


    if (typeof body?.detail === "string" && body.detail.trim()) {
      return body.detail.trim();
    }
    if (
      body?.detail &&
      typeof body.detail === "object" &&
      "message" in body.detail &&
      typeof body.detail.message === "string" &&
      body.detail.message.trim()
    ) {
      return body.detail.message.trim();
    }
  } catch {
    // ignore
  }


  return fallback;
}


/* =========================================================
   GET Status
========================================================= */

async function fetchCheckinStatus(
  userId: string,
): Promise<CheckinStatus> {
  const params =
    new URLSearchParams({
      user_id:
        userId,
    });


  const response =
    await apiFetch(
      `${API_BASE_URL}/api/checkin/status?${params.toString()}`,
      {
        method:
          "GET",

        cache:
          "no-store",
      },
    );


  if (
    !response.ok
  ) {
    throw new Error(
      await getErrorMessage(
        response,
        "取得簽到狀態失敗",
      ),
    );
  }


  return response.json();
}


/* =========================================================
   GET Reviewed Daily Content
========================================================= */

async function fetchDailyContents(): Promise<
  DailyContentsResponse
> {
  const response = await apiFetch(
    `${API_BASE_URL}/api/daily-contents`,
    {
      method: "GET",
      cache: "no-store",
    },
  );

  if (!response.ok) {
    throw new Error(
      await getErrorMessage(
        response,
        "取得每日內容失敗",
      ),
    );
  }

  return response.json();
}


/* =========================================================
   POST Check-in
========================================================= */

export async function submitDailyCheckin(
  userId: string,
): Promise<DailyCheckinResponse> {
  const params =
    new URLSearchParams({
      user_id:
        userId,
    });


  const response =
    await apiFetch(
      `${API_BASE_URL}/api/checkin?${params.toString()}`,
      {
        method:
          "POST",
      },
    );


  if (
    !response.ok
  ) {
    throw new Error(
      await getErrorMessage(
        response,
        "每日簽到失敗",
      ),
    );
  }


  invalidateQuery("checkin:status:" + userId.trim());
  invalidateQuery("checkin:history:" + userId.trim());
  return response.json();
}


/* =========================================================
   GET History
========================================================= */

async function fetchCheckinHistory(
  userId: string,
): Promise<CheckinHistoryResponse> {
  const params =
    new URLSearchParams({
      user_id:
        userId,
    });


  const response =
    await apiFetch(
      `${API_BASE_URL}/api/checkin/history?${params.toString()}`,
      {
        method:
          "GET",

        cache:
          "no-store",
      },
    );


  if (
    !response.ok
  ) {
    throw new Error(
      await getErrorMessage(
        response,
        "取得簽到紀錄失敗",
      ),
    );
  }


  return response.json();
}



export function getCheckinStatus(userId: string): Promise<CheckinStatus> {
  const normalizedUserId = userId.trim();
  return queryCached(
    `checkin:status:${normalizedUserId}`,
    () => fetchCheckinStatus(normalizedUserId),
    30_000,
  );
}

export function getDailyContents(): Promise<DailyContentsResponse> {
  return queryCached("checkin:daily-contents", fetchDailyContents, 5 * 60_000);
}

export function getCheckinHistory(userId: string): Promise<CheckinHistoryResponse> {
  const normalizedUserId = userId.trim();
  return queryCached(
    `checkin:history:${normalizedUserId}`,
    () => fetchCheckinHistory(normalizedUserId),
    60_000,
  );
}
