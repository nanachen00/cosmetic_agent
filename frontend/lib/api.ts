import { API_ORIGIN } from "@/lib/api-origin";
import { invalidateQuery, queryCached } from "@/lib/query-cache";

const API_BASE = API_ORIGIN;
const DEFAULT_API_TIMEOUT_MS = 30_000;

type ApiRequestInit = RequestInit & { timeoutMs?: number };

function apiFetch(input: RequestInfo | URL, init: ApiRequestInit = {}) {
  const { timeoutMs = DEFAULT_API_TIMEOUT_MS, signal, ...requestInit } = init;
  const timeoutSignal = AbortSignal.timeout(timeoutMs);
  const combinedSignal = signal
    ? AbortSignal.any([signal, timeoutSignal])
    : timeoutSignal;
  return fetch(input, {
    ...requestInit,
    credentials: "include",
    signal: combinedSignal,
  });
}

function chatRequestId(explicit?: string) {
  if (explicit) return explicit;
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  throw new Error("Secure request identifiers are unavailable");
}


/* =========================================================
   Frontend Quiz Types
========================================================= */

export type QuizOption = {
  id: string;
  label: string;
  description?: string;
};

export type QuizQuestion = {
  id: string;
  question: string;
  options: QuizOption[];
};

export type QuizAnswerMap =
  Record<string, string>;


/* =========================================================
   Backend Question Types
========================================================= */

type BackendQuizAnswer = {
  answer_id: string;
  label: string;
  description?: string;
};

type BackendQuizQuestion = {
  question_id: string;
  question: string;
  answers: BackendQuizAnswer[];
};

type QuizQuestionsResponse = {
  ok: boolean;
  questions: BackendQuizQuestion[];
};


/* =========================================================
   Submit Response
========================================================= */

export type QuizSubmitResponse = {
  ok: boolean;

  event_id?: string;
  event_mongo_id?: string;

  quiz_result_id?: string;
  quiz_result_mongo_id?: string;

  quiz_version?: string;

  primary_type?: string;
  secondary_type?: string;

  scores?: Record<
    string,
    number
  >;

  confidence?: number;
};


/* =========================================================
   Latest Quiz Result
========================================================= */

export type LatestQuizResult = {
  quiz_result_id?: string;

  quiz_version?: string;

  primary_type?: string;

  secondary_type?: string;

  scores?: Record<
    string,
    number
  >;

  confidence?: number;

  completed_at?: string | null;
};

export type LatestQuizResponse = {
  ok: boolean;

  has_completed: boolean;

  result:
    | LatestQuizResult
    | null;
};


/* =========================================================
   GET Quiz Questions

   GET /api/quiz/questions
========================================================= */

async function fetchQuizQuestions(): Promise<
  QuizQuestion[]
> {
  const response = await apiFetch(
    `${API_BASE}/api/quiz/questions`,
    {
      method: "GET",
      cache: "no-store",
    }
  );

  if (!response.ok) {
    const text =
      "Request failed";

    throw new Error(
      text ||
        `取得測驗題目失敗 (${response.status})`
    );
  }

  const data: QuizQuestionsResponse =
    await response.json();

  if (
    !data.ok ||
    !Array.isArray(
      data.questions
    )
  ) {
    throw new Error(
      "後端回傳的測驗題目格式不正確"
    );
  }

  /*
   * Backend
   *
   * question_id
   * answers
   * answer_id
   *
   * ↓
   *
   * Frontend
   *
   * id
   * options
   * id
   */

  return data.questions.map(
    (question) => ({
      id:
        question.question_id,

      question:
        question.question,

      options:
        question.answers.map(
          (answer) => ({
            id:
              answer.answer_id,

            label:
              answer.label,

            description:
              answer.description,
          })
        ),
    })
  );
}


/* =========================================================
   POST Quiz

   POST /api/quiz/submit
========================================================= */

export async function submitQuiz(
  userId: string,
  selectedAnswers: QuizAnswerMap,
  sessionId?: string,
  messageId?: string
): Promise<QuizSubmitResponse> {
  const response = await apiFetch(
    `${API_BASE}/api/quiz/submit`,
    {
      method: "POST",

      headers: {
        "Content-Type":
          "application/json",
      },

      body: JSON.stringify({
        user_id:
          userId,

        selected_answers:
          selectedAnswers,

        session_id:
          sessionId ?? null,

        message_id:
          messageId ?? null,
      }),
    }
  );

  if (!response.ok) {
    const text =
      "Request failed";

    throw new Error(
      text ||
        `送出測驗失敗 (${response.status})`
    );
  }

  const data: QuizSubmitResponse =
    await response.json();

  if (!data.ok) {
    throw new Error(
      "測驗結果儲存失敗"
    );
  }

  invalidateQuery("quiz:latest:" + userId.trim());
  return data;
}


/* =========================================================
   GET Latest Quiz

   GET /api/quiz/latest
========================================================= */

async function fetchLatestQuizResult(
  userId: string
): Promise<LatestQuizResponse> {
  const response = await apiFetch(
    `${API_BASE}/api/quiz/latest?user_id=${encodeURIComponent(
      userId
    )}`,
    {
      method: "GET",
      cache: "no-store",
    }
  );

  if (!response.ok) {
    const text =
      "Request failed";

    throw new Error(
      text ||
        `取得消費人格失敗 (${response.status})`
    );
  }

  const data: LatestQuizResponse =
    await response.json();

  if (!data.ok) {
    throw new Error(
      "無法取得消費人格資料"
    );
  }

  return data;
}

/* =========================================================
   Root Agent Chat + Profile/Checkin/SkinRecord（與 quiz 並存）
========================================================= */
import type {
  ChatCard,
  ConversationLimitStatus,
  ProfileForm,
  QuickReply,
  RootAgentResponse,
  RootRoute,
  SkinRecordPayload,
} from "@/types";
export type { ConversationLimitStatus } from "@/types";

const USE_MOCK = process.env.NEXT_PUBLIC_USE_MOCK === "true";

export function classifyLocally(message: string): RootRoute {
  const text = message.toLowerCase();
  if (/產品|推薦|保濕|精華|乳液|防曬|化妝水|面膜|哪一款|找商品/.test(text)) return "product_search";
  if (/記錄|紀錄|今天|出油|乾燥|泛紅|下巴|臉頰|額頭|嚴重度/.test(text)) return "skin_record";
  if (/怎麼辦|諮詢|刺痛|發癢|過敏|脫皮|痘痘|粉刺|肌膚問題/.test(text)) return "skin_consult";
  return "clarify";
}

export type ChatHistoryMessage = {
  created_at?: string;
  id: string;
  role: "user" | "assistant";
  content: string;
  quick_replies?: QuickReply[];
  quickReplies?: QuickReply[];
  cards?: ChatCard[];
};



export type ChatHistoryState = {
  messages: ChatHistoryMessage[];
  conversation_limit: ConversationLimitStatus | null;
};

export class ConversationLimitReachedError extends Error {
  readonly limit: ConversationLimitStatus;

  constructor(limit: ConversationLimitStatus, message?: string) {
    super(message || limit.message || "對話內容已額滿，請開新對話。");
    this.name = "ConversationLimitReachedError";
    this.limit = limit;
  }
}

export class ChatRequestRejectedError extends Error {
  readonly status: number;
  readonly code?: string;
  readonly retryAfterSeconds?: number;

  constructor(
    message: string,
    status: number,
    options: {
      code?: string;
      retryAfterSeconds?: number;
    } = {},
  ) {
    super(message);
    this.name = "ChatRequestRejectedError";
    this.status = status;
    this.code = options.code;
    this.retryAfterSeconds = options.retryAfterSeconds;
  }
}

function isConversationLimitStatus(value: unknown): value is ConversationLimitStatus {
  return Boolean(value && typeof value === "object" && "blocked" in value);
}


export type ConversationSummary = {
  conversation_id: string;
  title: string;
  created_at?: string;
  updated_at?: string;
};

export type ConversationPage = {
  conversations: ConversationSummary[];
  total: number;
  limit: number;
  offset: number;
  hasMore: boolean;
  maxConversations: number;
};

type ConversationListOptions = {
  limit?: number;
  offset?: number;
  force?: boolean;
};

/* =========================================================
 Conversation List

 GET /api/conversations
========================================================= */

export async function listConversations(
  userId: string,
  options: ConversationListOptions = {},
): Promise<ConversationPage> {
  const limit = Math.min(50, Math.max(1, options.limit ?? 20));
  const offset = Math.max(0, options.offset ?? 0);
  if (USE_MOCK) {
    return { conversations: [], total: 0, limit, offset, hasMore: false, maxConversations: 100 };
  }
  const normalizedUserId = userId.trim();
  const cacheKey = "conversations:" + normalizedUserId + ":" + limit + ":" + offset;
  if (options.force) invalidateQuery(cacheKey);
  return queryCached(cacheKey, async () => {
    const params = new URLSearchParams({
      user_id: normalizedUserId,
      limit: String(limit),
      offset: String(offset),
    });
    const response = await apiFetch(
      API_BASE + "/api/conversations?" + params.toString(),
      { method: "GET", cache: "no-store" },
    );
    if (!response.ok) throw new Error("Conversation list failed: " + response.status);
    const data = await response.json() as {
      conversations?: ConversationSummary[];
      total?: number;
      limit?: number;
      offset?: number;
      has_more?: boolean;
      max_conversations?: number;
    };
    const conversations = Array.isArray(data.conversations) ? data.conversations : [];
    const total = Number.isFinite(data.total) ? Number(data.total) : conversations.length;
    return {
      conversations,
      total,
      limit: Number(data.limit ?? limit),
      offset: Number(data.offset ?? offset),
      hasMore: Boolean(data.has_more),
      maxConversations: Number(data.max_conversations ?? 100),
    };
  }, 20_000);
}


/* =========================================================
   Create Conversation

   POST /api/conversations
========================================================= */

export async function createConversation(
  _userId: string,
): Promise<ConversationSummary> {
  // A new chat is a local draft; only a successful user send persists it.
  return { conversation_id: crypto.randomUUID(), title: "新對話" };
}

export async function renameConversation(
  userId: string,
  conversationId: string,
  title: string,
): Promise<ConversationSummary> {
  const response = await apiFetch(
    `${API_BASE}/api/conversations/${encodeURIComponent(conversationId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, title }),
    },
  );
  if (!response.ok) throw new Error("重新命名失敗，請稍後重試。");
  const data = await response.json() as { conversation: ConversationSummary };
  invalidateQuery("conversations:" + userId.trim() + ":");
  return data.conversation;
}


/* =========================================================
   Delete Conversation

   DELETE /api/conversations/{conversation_id}
========================================================= */

export async function deleteConversation(
  userId: string,
  conversationId: string,
): Promise<void> {
  if (USE_MOCK) {
    return;
  }

  const normalizedUserId =
    (
      userId
      || ""
    ).trim();

  const normalizedConversationId =
    (
      conversationId
      || ""
    ).trim();

  if (!normalizedUserId) {
    throw new Error(
      "缺少登入 user_id",
    );
  }

  if (!normalizedConversationId) {
    throw new Error(
      "缺少 conversation_id",
    );
  }

  const params =
    new URLSearchParams({
      user_id:
        normalizedUserId,
    });

  const response =
    await apiFetch(
      `${API_BASE}/api/conversations/${
        encodeURIComponent(
          normalizedConversationId,
        )
      }?${params.toString()}`,
      {
        method:
          "DELETE",
      },
    );

  if (!response.ok) {
    const body =
      "Request failed";

    throw new Error(
      body
      || (
        "Delete conversation failed: "
        + response.status
      ),
    );
  }
  invalidateQuery("conversations:" + normalizedUserId + ":");
}


/* =========================================================
   Get Chat History

   GET /api/chat/history
========================================================= */

export async function getChatHistoryState(
  userId: string,
  conversationId:
    string = "default",
  lastn = 100,
): Promise<ChatHistoryState> {
  if (USE_MOCK) {
    return { messages: [], conversation_limit: null };
  }

  const params =
    new URLSearchParams({
      user_id:
        userId,

      conversation_id:
        conversationId
        || "default",

      lastn:
        String(lastn),
    });

  const response =
    await apiFetch(
      `${API_BASE}/api/chat/history?${params.toString()}`,
      {
        method:
          "GET",

        cache:
          "no-store",
      },
    );

  if (!response.ok) {
    const body =
      "Request failed";

    throw new Error(
      body
      || (
        "Chat history failed: "
        + response.status
      ),
    );
  }

  const data =
    await response.json() as {
      ok?: boolean;
      user_id?: string;
      conversation_id?: string;
      messages?:
        ChatHistoryMessage[];
    conversation_limit?: ConversationLimitStatus;
    };

  const messages = Array.isArray(
    data.messages,
  )
    ? data.messages.map(
        (message) => ({
          ...message,
          quickReplies:
            message.quickReplies ??
            message.quick_replies,
        }),
      )
    : [];

  const limit =
    isConversationLimitStatus(data.conversation_limit)
      ? data.conversation_limit
      : null;

  return { messages, conversation_limit: limit };
}

export async function getChatHistory(
  userId: string,
  conversationId: string = "default",
  lastn = 100,
): Promise<ChatHistoryMessage[]> {
  return (
    await getChatHistoryState(
      userId,
      conversationId,
      lastn,
    )
  ).messages;
}


export type ChatJobStatus = {
  requestId: string;
  status: "running" | "completed" | "failed";
  conversationId: string;
  result: RootAgentResponse | null;
  error: string | null;
};

export async function getChatJobStatus(
  userId: string,
  requestId: string,
  signal?: AbortSignal,
): Promise<ChatJobStatus | null> {
  if (USE_MOCK) return null;

  const params = new URLSearchParams({ user_id: userId });
  const response = await apiFetch(
    API_BASE +
      "/api/chat/jobs/" +
      encodeURIComponent(requestId) +
      "?" +
      params.toString(),
    { method: "GET", cache: "no-store", signal },
  );
  if (response.status === 404) return null;
  if (!response.ok) {
    throw new Error("Chat request status failed: " + response.status);
  }
  return (await response.json()) as ChatJobStatus;
}

/* ==================================================
   Send Root Agent Message

   POST /api/chat/message
========================================================= */

export async function sendRootMessage(
  input: {
    message: string;
    userId: string;
    conversationId?:
      string;
    flow?: string;
    token?: string;
    action?: string;
    requestId?: string;
  },
): Promise<RootAgentResponse> {
  const conversationId =
    (
      input.conversationId
      || "default"
    ).trim()
    || "default";

  if (USE_MOCK) {
    await new Promise(
      resolve =>
        setTimeout(
          resolve,
          420,
        ),
    );

    return {
      route:
        classifyLocally(
          input.message,
        ),

      sessionId:
        conversationId,

      reply:
        "（mock）",
    };
  }

  const response =
    await apiFetch(
      `${API_BASE}/api/chat/message`,
      {
        method:
          "POST",
      keepalive: true,

        headers: {
          "Content-Type":
            "application/json",
          "Idempotency-Key":
            chatRequestId(input.requestId),

          ...(
            input.token
              ? {
                  Authorization:
                    `Bearer ${input.token}`,
                }
              : {}
          ),
        },

        body:
          JSON.stringify({
            message:
              input.message,

            user_id:
              input.userId,

            conversation_id:
              conversationId,

            flow:
              input.flow,
            action: input.action,
          }),
      },
    );

  if (!response.ok) {
    const body =
      "Request failed";

    throw new Error(
      body
      || (
        "Root Agent request failed: "
        + response.status
      ),
    );
  }

  const data = (await response.json()) as RootAgentResponse;

  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent("blink-conversations-updated", {
        detail: { conversationId: (input.conversationId || "default").trim() || "default" },
      }),
    );
  }

  return data;
}


export async function sendRootMessageStream(
  input: {
    message: string;
    userId: string;
    conversationId?: string;
    flow?: string;
    token?: string;
    action?: string;
    requestId?: string;
    signal?: AbortSignal;
    waitUntilResumed?: () => Promise<void>;
  },
  onDelta: (text: string) => void,
): Promise<RootAgentResponse> {
  const conversationId =
    (input.conversationId || "default").trim() || "default";

  if (USE_MOCK) {
    const result = await sendRootMessage(input);
    onDelta(result.reply || "");
    return result;
  }

  const response = await apiFetch(
    API_BASE + "/api/chat/message/stream",
    {
      method: "POST",
      timeoutMs: 180_000,
      signal: input.signal,
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": chatRequestId(input.requestId),
        ...(input.token
          ? { Authorization: "Bearer " + input.token }
          : {}),
      },
      body: JSON.stringify({
        message: input.message,
        user_id: input.userId,
        conversation_id: conversationId,
        flow: input.flow,
            action: input.action,
      }),
    },
  );

  if (!response.ok) {
    const payload = (await response
      .json()
      .catch(() => null)) as
      | {
          detail?:
            | string
            | {
                code?: string;
                message?: string;
                conversation_limit?: unknown;
              };
        }
      | null;
    const detail = payload?.detail;
    const detailObject =
      detail && typeof detail === "object"
        ? detail
        : null;
    const errorMessage =
      typeof detail === "string"
        ? detail
        : detailObject?.message;
    if (
      response.status === 409 &&
      detailObject?.code === "conversation_limit_reached" &&
      isConversationLimitStatus(detailObject.conversation_limit)
    ) {
      throw new ConversationLimitReachedError(
        detailObject.conversation_limit,
        detailObject.message,
      );
    }

    const retryAfterHeader = response.headers.get("Retry-After");
    const retryAfterValue = retryAfterHeader
      ? Number.parseInt(retryAfterHeader, 10)
      : Number.NaN;
    const retryAfterSeconds = Number.isFinite(retryAfterValue)
      ? Math.max(1, retryAfterValue)
      : undefined;

    throw new ChatRequestRejectedError(
      errorMessage || "Streaming chat request failed: " + response.status,
      response.status,
      {
        code: detailObject?.code,
        retryAfterSeconds,
      },
    );
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalResponse: RootAgentResponse | null = null;

  const consumeEvent = (block: string) => {
    let eventName = "message";
    const dataLines: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("event:")) {
        eventName = line.slice(6).trim();
      } else if (line.startsWith("data:")) {
        dataLines.push(line.slice(5).trimStart());
      }
    }
    if (!dataLines.length) return;
    const payload = JSON.parse(dataLines.join("\n")) as {
      text?: string;
      message?: string;
    } & RootAgentResponse;
    if (eventName === "delta" && payload.text) {
      onDelta(payload.text);
    } else if (eventName === "done") {
      finalResponse = payload;
    } else if (eventName === "error") {
      throw new Error(payload.message || "串流回覆失敗");
    }
  };

  while (true) {
    await input.waitUntilResumed?.();
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    buffer = buffer.replace(/\r\n/g, "\n");
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      consumeEvent(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
    }
    if (done) break;
  }

  if (!finalResponse) {
    throw new Error("串流連線已結束，但沒有收到完成事件");
  }
  if (typeof window !== "undefined") {
    window.dispatchEvent(
      new CustomEvent("blink-conversations-updated", {
        detail: { conversationId: (input.conversationId || "default").trim() || "default" },
      }),
    );
  }
  return finalResponse;
}


/* =========================================================
   Clear One Conversation

   POST /api/v1/session/clear
========================================================= */

export async function clearSession(
  userId: string,
  conversationId:
    string = "default",
): Promise<void> {
  if (USE_MOCK) {
    return;
  }

  const params =
    new URLSearchParams({
      user_id:
        userId,

      conversation_id:
        conversationId
        || "default",
    });

  const response =
    await apiFetch(
      `${API_BASE}/api/v1/session/clear?${params.toString()}`,
      {
        method:
          "POST",
      },
    );

  if (!response.ok) {
    const body =
      "Request failed";

    throw new Error(
      body
      || (
        "Clear session failed: "
        + response.status
      ),
    );
  }
}


export async function saveProfile(
  profile: ProfileForm,
  userId: string,
  token?: string,
): Promise<void> {
  if (!userId.trim()) {
    throw new Error("Profile save failed: missing user id");
  }

  if (USE_MOCK) {
    await new Promise((resolve) => setTimeout(resolve, 280));
    return;
  }

  const response = await apiFetch(`${API_BASE}/api/user/profile`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body: JSON.stringify({
      ...profile,
      birthday: profile.birthday || undefined,
      user_id: userId.trim(),
    }),
  });

  if (!response.ok) {
    let message = `儲存基本資料失敗 (${response.status})`;
    try {
      const data = (await response.json()) as { detail?: string };
      message = "Request failed";
    } catch {
      // Keep the status-based message when the response is not JSON.
    }
    throw new Error(message);
  }
  invalidateQuery("profile:" + userId.trim());
}

async function fetchProfile(
  userId: string,
): Promise<Partial<ProfileForm>> {
  if (!userId.trim()) {
    throw new Error("無法讀取基本資料：缺少會員識別碼");
  }

  const response = await apiFetch(
    `${API_BASE}/api/v1/profile?user_id=${encodeURIComponent(userId.trim())}`,
    {
      method: "GET",
      cache: "no-store",
    },
  );

  if (!response.ok) {
    let message = `讀取基本資料失敗 (${response.status})`;
    try {
      const data = (await response.json()) as { detail?: string };
      message = "Request failed";
    } catch {
      // Keep the status-based message when the response is not JSON.
    }
    throw new Error(message);
  }

  const data = (await response.json()) as {
    ok?: boolean;
    profile?: Partial<ProfileForm>;
  };
  return data.profile || {};
}

export async function submitDailyCheckin(token?: string): Promise<{ points: number }> {
  if (USE_MOCK) { await new Promise((r) => setTimeout(r, 260)); return { points: 10 }; }
  const response = await apiFetch(`${API_BASE}/api/checkin`, {
    method: "POST",
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  });
  if (!response.ok) throw new Error(`Check-in failed: ${response.status}`);
  return (await response.json()) as { points: number };
}

export async function saveSkinRecord(payload: SkinRecordPayload, token?: string): Promise<void> {
  if (USE_MOCK) { await new Promise((r) => setTimeout(r, 360)); return; }
  const response = await apiFetch(`${API_BASE}/api/skin-records/frontend`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(`Skin record failed: ${response.status}`);
}


export function getQuizQuestions(): Promise<QuizQuestion[]> {
  return queryCached("quiz:questions", fetchQuizQuestions, 5 * 60_000);
}

export function getLatestQuizResult(userId: string): Promise<LatestQuizResponse> {
  const normalizedUserId = userId.trim();
  return queryCached(
    `quiz:latest:${normalizedUserId}`,
    () => fetchLatestQuizResult(normalizedUserId),
    60_000,
  );
}

export function getProfile(userId: string): Promise<Partial<ProfileForm>> {
  const normalizedUserId = userId.trim();
  return queryCached(
    `profile:${normalizedUserId}`,
    () => fetchProfile(normalizedUserId),
    60_000,
  );
}
