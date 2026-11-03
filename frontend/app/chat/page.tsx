"use client";

import {
  Fragment,
  FormEvent,
  Suspense,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  useRouter,
  useSearchParams,
} from "next/navigation";

import {
  CHAT_FLOW_CONFIG,
  isChatFlow,
  type ChatFlow,
} from "@/lib/chat-flow";

import {
  sendRootMessageStream,
  clearSession,
  getChatHistoryState,
  getChatJobStatus,
  createConversation,
  classifyLocally,
  ConversationLimitReachedError,
  ChatRequestRejectedError,
} from "@/lib/api";

import type {
  ChatCard,
  ConversationLimitStatus,
  QuickReply,
} from "@/types";

import {
  readSession,
} from "@/lib/storage";
import { resolveCurrentUserId } from "@/lib/auth-client";

import CustomerQuiz from "@/components/CustomerQuiz";


/* =========================================================
   User ID
========================================================= */


const CHAT_PENDING_KEY_PREFIX =
  "blink_chat_pending_v1";
const CHAT_PENDING_TTL_MS =
  5 * 60 * 1000;

function isConversationBootstrapMessage(message: string): boolean {
  return new Set([
    "cycle_skin_tracking",
    "我要記錄膚況",
    "我要紀錄膚況",
    "我要記錄今天的膚況",
    "我要紀錄今天的膚況",
    "開始記錄膚況",
    "開始紀錄膚況",
  ]).has(message.trim());
}

function previewConversationTitle(
  message: string,
  flow: ChatFlow | null,
): string {
  const normalized = message.trim().replace(/\s+/g, " ");
  const core = normalized.slice(0, 12) || "新對話";
  const inferredFlow = flow && flow !== "auto"
    ? flow
    : /記錄|紀錄|追蹤|今天.*膚況|膚況.*今天/.test(normalized)
      ? "skin_record"
      : /找|推薦|挑|比較|預算|價格|產品|商品|面膜|防曬|洗面|精華|乳液|化妝水/.test(normalized)
        ? "product_search"
        : /為什麼|怎麼|如何|成分|痘|粉刺|泛紅|乾燥|脫皮|刺痛|敏感|出油|毛孔|搔癢|斑|疤/.test(normalized)
          ? "skin_consult"
          : "auto";
  const prefix = inferredFlow === "skin_record"
    ? "膚況紀錄"
    : inferredFlow === "skin_consult"
      ? "膚況諮詢"
      : inferredFlow === "product_search"
        ? "找產品"
        : inferredFlow === "customer_quiz"
          ? "消費測驗"
          : "";
  return prefix ? prefix + "｜" + core : core;
}

function getChatFailureMessage(error: unknown): string {
  if (
    error instanceof ChatRequestRejectedError &&
    error.status === 429
  ) {
    const waitText = error.retryAfterSeconds
      ? `，請約 ${error.retryAfterSeconds} 秒後再試`
      : "，請稍候再試";
    const previousReplyStillRunning =
      error.code === "chat_request_in_progress" ||
      /already being generated|still.*generat/i.test(error.message);

    return previousReplyStillRunning
      ? `上一則回覆仍在處理中${waitText}。`
      : `訊息傳送太頻繁${waitText}。`;
  }

  return "抱歉，系統忙碌中，請稍後再試一次 🙏";
}

const CHAT_ACTIVE_RETRY_MAX_WAIT_MS = 60_000;

function isPreviousReplyStillRunning(error: unknown): error is ChatRequestRejectedError {
  return (
    error instanceof ChatRequestRejectedError &&
    error.status === 429 &&
    (error.code === "chat_request_in_progress" ||
      /already being generated|still.*generat/i.test(error.message))
  );
}

function waitForAbortableDelay(ms: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) {
    return Promise.reject(new DOMException("Aborted", "AbortError"));
  }

  return new Promise((resolve, reject) => {
    const timeoutId = window.setTimeout(() => {
      signal?.removeEventListener("abort", handleAbort);
      resolve();
    }, ms);
    const handleAbort = () => {
      window.clearTimeout(timeoutId);
      reject(new DOMException("Aborted", "AbortError"));
    };
    signal?.addEventListener("abort", handleAbort, { once: true });
  });
}

async function sendRootMessageStreamWhenAvailable(
  input: Parameters<typeof sendRootMessageStream>[0],
  onDelta: Parameters<typeof sendRootMessageStream>[1],
) {
  const requestId = input.requestId ?? crypto.randomUUID();
  const startedAt = Date.now();

  for (;;) {
    try {
      return await sendRootMessageStream({ ...input, requestId }, onDelta);
    } catch (error) {
      if (!isPreviousReplyStillRunning(error)) {
        throw error;
      }

      const retryDelayMs = Math.max(
        1_000,
        (error.retryAfterSeconds ?? 5) * 1_000,
      );
      if (Date.now() - startedAt + retryDelayMs > CHAT_ACTIVE_RETRY_MAX_WAIT_MS) {
        throw error;
      }
      await waitForAbortableDelay(retryDelayMs, input.signal);
    }
  }
}

type PendingChatRequest = {
  message: string;
  displayMessage?: string;
  clientMessageId?: string;
  requestId?: string;
  startedAt: number;
};

function getPendingChatKey(
  chatHistoryKey: string,
): string {
  return (
    CHAT_PENDING_KEY_PREFIX +
    ":" +
    chatHistoryKey
  );
}

function uniqueQuickReplies(replies: QuickReply[]): QuickReply[] {
  const seen = new Set<string>();

  return replies.filter((reply) => {
    if (seen.has(reply.value)) {
      return false;
    }

    seen.add(reply.value);
    return true;
  });
}

function readPendingChatRequest(
  key: string,
): PendingChatRequest | null {
  if (typeof window === "undefined") {
    return null;
  }

  try {
    const raw =
      window.sessionStorage.getItem(key);
    if (!raw) return null;

    const parsed = JSON.parse(
      raw,
    ) as Partial<PendingChatRequest>;
    if (
      typeof parsed.message !== "string" ||
      typeof parsed.startedAt !== "number" ||
      Date.now() - parsed.startedAt >
        CHAT_PENDING_TTL_MS
    ) {
      window.sessionStorage.removeItem(key);
      return null;
    }

    return {
      message: parsed.message,
      displayMessage:
        typeof parsed.displayMessage === "string"
          ? parsed.displayMessage
          : undefined,
      clientMessageId:
        typeof parsed.clientMessageId === "string"
          ? parsed.clientMessageId
          : undefined,
      startedAt: parsed.startedAt,
    };
  } catch {
    window.sessionStorage.removeItem(key);
    return null;
  }
}

function writePendingChatRequest(
  key: string,
  message: string,
  displayMessage?: string,
  clientMessageId?: string,
  requestId?: string,
): void {
  if (typeof window === "undefined") return;
  window.sessionStorage.setItem(
    key,
    JSON.stringify({
      message,
      displayMessage,
      clientMessageId,
      requestId,
      startedAt: Date.now(),
    } satisfies PendingChatRequest),
  );
}

function clearPendingChatRequest(
  key: string,
): void {
  if (typeof window === "undefined") return;
  window.sessionStorage.removeItem(key);
}



/* =========================================================
   Chat Message
========================================================= */

interface ChatMessage {
  id: string;
  created_at?: string;

  role:
    | "assistant"
    | "user";

  content: string;

  /* Friendly label shown for a backend quick-reply value. */
  requestContent?: string;

  quickReplies?: QuickReply[];

  cards?: ChatCard[];
}


/* =========================================================
   Message Date / Time

   - 訊息旁只顯示 24 小時制時間。
   - 每個日期群組只顯示一次置中的日期標籤。
========================================================= */

function parseMessageDate(
  value?: string,
): Date | null {
  if (!value) return null;

  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? null
    : date;
}


function localDayKey(
  date: Date,
): string {
  return [
    date.getFullYear(),
    String(date.getMonth() + 1).padStart(2, "0"),
    String(date.getDate()).padStart(2, "0"),
  ].join("-");
}


function localDayNumber(
  date: Date,
): number {
  return Date.UTC(
    date.getFullYear(),
    date.getMonth(),
    date.getDate(),
  );
}


function formatMessageDateLabel(
  value: string,
): string {
  const date = parseMessageDate(value);
  if (!date) return "";

  const today = new Date();
  const dayDifference = Math.round(
    (
      localDayNumber(today) -
      localDayNumber(date)
    ) /
      (24 * 60 * 60 * 1000),
  );

  if (dayDifference === 0) return "今天";
  if (dayDifference === 1) return "昨天";

  const weekday = date.toLocaleDateString(
    "zh-TW",
    { weekday: "short" },
  );

  return `${date.getMonth() + 1}/${date.getDate()}（${weekday}）`;
}


function formatMessageTime(
  value: string,
): string {
  const date = parseMessageDate(value);
  if (!date) return "";

  return date.toLocaleTimeString(
    "zh-TW",
    {
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    },
  );
}


function shouldShowDateDivider(
  messages: ChatMessage[],
  index: number,
): boolean {
  const currentDate = parseMessageDate(
    messages[index]?.created_at,
  );
  if (!currentDate) return false;

  const currentKey = localDayKey(currentDate);

  for (
    let previousIndex = index - 1;
    previousIndex >= 0;
    previousIndex -= 1
  ) {
    const previousDate = parseMessageDate(
      messages[previousIndex]?.created_at,
    );

    if (previousDate) {
      return localDayKey(previousDate) !== currentKey;
    }
  }

  return true;
}


/* =========================================================
   Internal Commands

   這些值是前後端控制流程使用的指令，不是真正的使用者訊息。
   不顯示成對話泡泡，也不寫入 sessionStorage。
========================================================= */

const FACE_ZONE_LABELS: Record<string, string> = {
  t_zone: "T字部位（額頭、鼻子）",
  left_cheek: "左臉頰區（含左眼周）",
  right_cheek: "右臉頰區（含右眼周）",
  chin: "下巴區（嘴周、人中、下巴）",
};

const INTERNAL_ACTION_LABELS: Record<string, string> = {
  confirm_yes: "✅ 對，就是這樣",
  confirm_no: "❌ 需要調整",
  cycle_more: "調整推薦條件",
  cycle_replace_batch: "調整推薦條件",
  cycle_change_category: "調整推薦條件",
  cycle_change_condition: "調整推薦條件",
  cycle_rank_feedback: "排序不對，幫我調整",
};

const HIDDEN_INTERNAL_USER_MESSAGES =
  new Set([
    "cycle_skin_tracking",
  ]);

const SKIN_RECORD_ENTRY_PROMPT =
  "嗨～一起看看今天的肌膚在說什麼吧 ♡ 最近有哪個變化讓你特別在意？你可以直接說完整一點，例如「這兩天下巴冒了幾顆痘，摸起來有點痛」。";


const SKIN_RECORD_QUICK_REPLIES: QuickReply[] = [
  { label: "痘痘", value: "痘痘" },
  { label: "乾燥", value: "乾燥" },
  { label: "泛紅", value: "泛紅" },
  { label: "出油", value: "出油" },
  { label: "搔癢", value: "搔癢" },
  { label: "斑點", value: "斑點" },
  { label: "痘疤", value: "痘疤" },
  { label: "其他狀況…", value: "其他狀況…" },
]

function isVisibleChatMessage(
  message: ChatMessage,
): boolean {
  const content = message.content.trim();

  return !(
    message.role === "user" &&
    (
      HIDDEN_INTERNAL_USER_MESSAGES.has(content) ||
      (
        content.startsWith("cycle_") &&
        !(content in INTERNAL_ACTION_LABELS)
      )
    )
  );
}


function normalizeInternalActionMessage(
  message: ChatMessage,
): ChatMessage {
  if (message.role !== "user") {
    return message;
  }

  const action = message.content.trim();
  const zoneValues = action.split(/[、,]/).map((value) => value.trim());
  const zoneLabel = zoneValues.length > 0 && zoneValues.every((value) => value in FACE_ZONE_LABELS)
    ? zoneValues.map((value) => FACE_ZONE_LABELS[value]).join("、")
    : "";
  const label = INTERNAL_ACTION_LABELS[action] || zoneLabel;

  return label
    ? {
        ...message,
        content: label,
        requestContent: action,
      }
    : message;
}


function normalizeMessageLinks(content: string): string {
  const repairedMarkdownLinks = content.replace(
    /\[([^\]\n]+)\]\(\s*(https?)([\s\S]*?)\)/gi,
    (original, label: string, scheme: string, rawTarget: string) => {
      const compactTarget = rawTarget.replace(/[\s\\]+/g, "");
      const slashPairIndex = compactTarget.indexOf("//");

      if (slashPairIndex < 0) {
        return original;
      }

      const url = `${scheme.toLowerCase()}:${compactTarget.slice(slashPairIndex)}`;

      try {
        new URL(url);
        return `[${label}](${url})`;
      } catch {
        return original;
      }
    },
  );

  return repairedMarkdownLinks.replace(
    /(https?)[\s\\:]*\/[\s\\]*\/[\s\\]*/gi,
    "$1://",
  );
}


function isExternalTemplateArtifact(content: string): boolean {
  return /(?:amazonProductReview|product[\\_]*\.product[\\_]*review|translateModel|name[\\_]*translations|reviews['"]?\s*\|\s*translate)/i.test(
    content,
  );
}


function sanitizeExternalMessageContent(content: string): string {
  return content
    .split(/\r?\n/)
    .filter((line) => !isExternalTemplateArtifact(line))
    .join("\n");
}


function renderInlineContent(content: string, keyPrefix: string) {
  const normalizedContent = normalizeMessageLinks(
    sanitizeExternalMessageContent(content),
  );
  const labeledRawLink = normalizedContent.match(
    /^(查看商品資料|商品資料|參考資料|資料來源|來源)\s*[：:]\s*(https?:\/\/[^\s，。；;]+)\s*$/,
  );

  if (labeledRawLink) {
    const label = labeledRawLink[1];
    const linkText = label.includes("商品")
      ? "查看商品資料 ↗"
      : "查看來源";

    return label.includes("商品")
      ? [
          <a
            className="chat-message-link"
            href={labeledRawLink[2]}
            key={keyPrefix + "-labeled-source"}
            rel="noopener noreferrer"
            target="_blank"
          >
            {linkText}
          </a>,
        ]
      : [
          <strong
            className="chat-message-emphasis"
            key={keyPrefix + "-source-label"}
          >
            {label}：
          </strong>,
          " ",
          <a
            className="chat-message-link"
            href={labeledRawLink[2]}
            key={keyPrefix + "-labeled-source"}
            rel="noopener noreferrer"
            target="_blank"
          >
            {linkText}
          </a>,
        ];
  }

  const detailMatch = normalizedContent.match(/^(品牌：|適合你的原因：|重點成分：|商品特色：)(.*)$/);
  if (detailMatch) {
    return [
      <strong className="chat-message-emphasis" key={keyPrefix + "-label"}>
        {detailMatch[1]}
      </strong>,
      ...renderInlineContent(detailMatch[2], keyPrefix + "-detail"),
    ];
  }
  return normalizedContent
    .split(/(\*\*[^*\n]+\*\*|「[^」\n]+」|\[[^\]\n]+\]\(https?:\/\/[^\s)]+\)|https?:\/\/[^\s，。；;)）\]}>]+)/g)
    .filter(Boolean)
    .map((part, index) => {
      if (part.startsWith("「") && part.endsWith("」")) {
        return (
          <strong className="chat-message-emphasis" key={keyPrefix + "-quote-" + index}>
            {part.slice(1, -1)}
          </strong>
        );
      }

      if (part.startsWith("**") && part.endsWith("**")) {
        return (
          <strong className="chat-message-emphasis" key={keyPrefix + "-bold-" + index}>
            {part.slice(2, -2)}
          </strong>
        );
      }

      const linkMatch = part.match(/^\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)$/);
      if (linkMatch) {
        return (
          <a
            className="chat-message-link"
            href={linkMatch[2]}
            key={keyPrefix + "-link-" + index}
            rel="noopener noreferrer"
            target="_blank"
          >
            {linkMatch[1]}
          </a>
        );
      }

      if (/^https?:\/\//.test(part)) {
        return (
          <a
            className="chat-message-link"
            href={part}
            key={keyPrefix + "-raw-link-" + index}
            rel="noopener noreferrer"
            target="_blank"
          >
            查看來源
          </a>
        );
      }

      return part;
    });
}

function renderMessageContent(content: string) {
  const lines = normalizeMessageLinks(
    sanitizeExternalMessageContent(content),
  ).split("\n");

  return lines.map((line, lineIndex) => {
    const isProductTitle = line.startsWith("### ");
    const lineContent = isProductTitle ? line.slice(4) : line;
    const renderedLine = renderInlineContent(lineContent, "line-" + lineIndex);

    return (
      <Fragment key={"message-line-" + lineIndex}>
        {isProductTitle ? (
          <span className="chat-product-title">{renderedLine}</span>
        ) : (
          renderedLine
        )}
        {lineIndex < lines.length - 1 ? <br /> : null}
      </Fragment>
    );
  });
}


/* =========================================================
   Readable Product Recommendation Copy

   後端仍維持原本的純文字回覆格式；前端只在辨識到商品推薦時，
   將商品名稱、價格、推薦重點與補充資訊排成易讀卡片。
   其他類型訊息沿用既有 renderMessageContent。
========================================================= */

interface ProductRecommendationBlock {
  number: string;
  title: string;
  lines: string[];
}


interface ParsedProductRecommendations {
  introduction: string[];
  products: ProductRecommendationBlock[];
  conclusion: string[];
  hasConclusionDivider: boolean;
}


const PRODUCT_RECOMMENDATION_FIELD_LABELS = new Set([
  "品牌",
  "類型",
  "價格",
  "售價",
  "適合你的原因",
  "推薦理由",
  "推薦重點",
  "商品介紹",
  "商品特色",
  "熱門依據",
  "參考資料",
  "資料來源",
  "來源",
  "查看商品資料",
  "商品資料",
]);


const PRODUCT_RECOMMENDATION_SOURCE_LABELS = new Set([
  "參考資料",
  "資料來源",
  "來源",
  "查看商品資料",
  "商品資料",
]);


function normalizeRecommendationStructure(line: string): string {
  let normalized = line.trim();

  normalized = normalized.replace(/^#{1,6}\s+/, "");
  normalized = normalized.replace(/^[-*]\s+/, "");

  if (normalized.startsWith("**") && normalized.endsWith("**")) {
    normalized = normalized.slice(2, -2).trim();
  }

  return normalized;
}


function parseProductRecommendations(
  content: string,
): ParsedProductRecommendations | null {
  const lines = normalizeMessageLinks(
    sanitizeExternalMessageContent(content),
  )
    .split(/\r?\n/)
    .map((line) => line.trim());
  const introduction: string[] = [];
  const products: ProductRecommendationBlock[] = [];
  const conclusion: string[] = [];
  let currentProduct: ProductRecommendationBlock | null = null;
  let hasConclusionDivider = false;
  let readingConclusion = false;

  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const structuralLine = normalizeRecommendationStructure(line);
    const numberedLine = structuralLine.match(
      /^(?:第\s*)?(\d+)\s*[.、．)）]\s*(.*)$/,
    );

    if (
      currentProduct &&
      /^[-_—─]{3,}$/.test(structuralLine)
    ) {
      hasConclusionDivider = true;
      readingConclusion = true;
      continue;
    }

    if (readingConclusion) {
      if (line) {
        conclusion.push(line);
      }
      continue;
    }

    if (numberedLine) {
      let title = numberedLine[2].trim();

      if (!title) {
        let titleIndex = index + 1;

        while (titleIndex < lines.length && !lines[titleIndex]) {
          titleIndex += 1;
        }

        title = normalizeRecommendationStructure(lines[titleIndex] ?? "");

        if (title) {
          index = titleIndex;
        }
      }

      if (!title) {
        return null;
      }

      currentProduct = {
        number: numberedLine[1],
        title,
        lines: [],
      };
      products.push(currentProduct);
      continue;
    }

    if (currentProduct) {
      if (line) {
        currentProduct.lines.push(line);
      }
    } else if (line) {
      introduction.push(line);
    }
  }

  const hasRecommendationFields = products.some((product) =>
    product.lines.some((line) => {
      const field = splitRecommendationField(line);
      return Boolean(
        field && PRODUCT_RECOMMENDATION_FIELD_LABELS.has(field.label),
      );
    }),
  );

  return products.length > 0 && hasRecommendationFields
    ? {
        introduction,
        products,
        conclusion,
        hasConclusionDivider,
      }
    : null;
}


function splitRecommendationField(line: string): {
  label: string;
  value: string;
} | null {
  const normalized = normalizeRecommendationStructure(line)
    .replace(/\*\*/g, "")
    .trim();
  const markdownLink = normalized.match(
    /^\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)$/,
  );
  if (markdownLink) {
    return {
      label: markdownLink[1].replace(/\s*↗\s*$/, "").trim(),
      value: markdownLink[2],
    };
  }

  const match = normalized.match(/^([^：:]+)[：:]\s*(.*)$/);

  return match
    ? {
        label: match[1].trim(),
        value: match[2].trim(),
      }
    : null;
}


function ProductRecommendationCard({
  product,
}: {
  product: ProductRecommendationBlock;
}) {
  const fields = product.lines
    .map(splitRecommendationField)
    .filter(
      (
        field,
      ): field is {
        label: string;
        value: string;
      } => field !== null,
    );
  const brandField = fields.find((field) => field.label === "品牌");
  const metadata = brandField?.value
    .split(/[，,|｜]/)
    .map((item) => item.trim())
    .filter(Boolean) ?? [];
  const priceIndex = metadata.findIndex((item) =>
    /NT\$|NTD|售價|價格/.test(item),
  );
  const price = priceIndex >= 0 ? metadata[priceIndex] : "";
  const tags = metadata.filter((_, index) => index !== priceIndex);
  const detailLines = product.lines.filter((line) => {
    const field = splitRecommendationField(line);

    if (field?.label === "品牌") {
      return false;
    }

    return !field || field.value.trim().length > 0;
  });

  return (
    <article className="product-recommendation-card">
      <header className="product-recommendation-card__header">
        <span
          className="product-recommendation-card__number"
          aria-label={`推薦商品 ${product.number}`}
        >
          {product.number}
        </span>

        <div className="product-recommendation-card__heading">
          <h3>{renderMessageContent(product.title)}</h3>

          {tags.length > 0 && (
            <div className="product-recommendation-card__tags">
              {tags.map((tag, index) => (
                <span key={`${tag}-${index}`}>
                  {index === 0 && brandField
                    ? `${brandField.label}：${tag}`
                    : tag}
                </span>
              ))}
            </div>
          )}
        </div>

        {price && (
          <strong className="product-recommendation-card__price">
            {price}
          </strong>
        )}
      </header>

      {detailLines.map((line, index) => {
        const field = splitRecommendationField(line);
        const isHighlight =
          field?.label === "適合你的原因" ||
          field?.label === "推薦理由";
        const isSource = Boolean(
          field && PRODUCT_RECOMMENDATION_SOURCE_LABELS.has(field.label),
        );
        const rawSourceUrl = isSource
          ? field?.value.match(/^https?:\/\/[^\s]+$/)?.[0] ?? ""
          : "";
        const className = isHighlight
          ? "product-recommendation-card__highlight"
          : isSource
            ? "product-recommendation-card__source-row"
            : "product-recommendation-card__description";

        return (
          <section
            className={className}
            key={`${line}-${index}`}
          >
            {field ? (
              <>
                <span>{field.label}</span>
                <p>
                  {rawSourceUrl ? (
                    <a
                      className="chat-message-link"
                      href={rawSourceUrl}
                      rel="noopener noreferrer"
                      target="_blank"
                    >
                      {field.label.includes("商品")
                        ? "查看商品資料 ↗"
                        : "查看來源"}
                    </a>
                  ) : (
                    renderMessageContent(field.value)
                  )}
                </p>
              </>
            ) : (
              <p>{renderMessageContent(line)}</p>
            )}
          </section>
        );
      })}
    </article>
  );
}


function ReadableAssistantMessage({
  content,
}: {
  content: string;
}) {
  const recommendations = parseProductRecommendations(content);

  if (!recommendations) {
    return <>{renderMessageContent(content)}</>;
  }

  return (
    <div className="product-recommendation-response">
      {recommendations.introduction.length > 0 && (
        <p className="product-recommendation-response__intro">
          {renderMessageContent(recommendations.introduction.join("\n"))}
        </p>
      )}

      <div className="product-recommendation-list">
        {recommendations.products.map((product) => (
          <ProductRecommendationCard
            key={`${product.number}-${product.title}`}
            product={product}
          />
        ))}
      </div>

      {(
        recommendations.hasConclusionDivider ||
        recommendations.conclusion.length > 0
      ) && (
        <div className="product-recommendation-response__conclusion">
          {recommendations.hasConclusionDivider && <hr />}
          {recommendations.conclusion.map((line, index) => (
            <p key={`${line}-${index}`}>
              {renderMessageContent(line)}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}


function dedupeConsecutiveMessages(
  messages: ChatMessage[],
): ChatMessage[] {
  const seenMessageIds =
    new Set<string>();
  let skinRecordEntrySeen =
    false;

  return messages.reduce<ChatMessage[]>(
    (result, originalMessage, index) => {
      const providedId =
        typeof originalMessage.id === "string"
          ? originalMessage.id.trim()
          : "";
      const messageId =
        providedId ||
        [
          "history",
          originalMessage.role,
          index,
        ].join("-");
      const message = {
        ...originalMessage,
        id: messageId,
      };

      if (seenMessageIds.has(messageId)) {
        return result;
      }

      if (
        message.role ===
          "assistant" &&
        message.content.trim() ===
          SKIN_RECORD_ENTRY_PROMPT
      ) {
        if (skinRecordEntrySeen) {
          return result;
        }

        skinRecordEntrySeen = true;
      }

      seenMessageIds.add(messageId);
      result.push(message);
      return result;
    },
    [],
  );
}


function mergeServerHistoryWithCachedPresentation(
  serverHistory: ChatMessage[],
  cachedHistory: ChatMessage[],
): ChatMessage[] {
  const usedCachedIndexes =
    new Set<number>();

  return serverHistory.map(
    (serverMessage) => {
      const cachedIndex =
        cachedHistory.findIndex(
          (cachedMessage, index) => {
            if (
              usedCachedIndexes.has(index) ||
              cachedMessage.role !==
                serverMessage.role
            ) {
              return false;
            }

            if (
              cachedMessage.id ===
              serverMessage.id
            ) {
              return true;
            }

            const cachedCanonicalContent =
              cachedMessage.role === "user"
                ? (
                    cachedMessage.requestContent ??
                    cachedMessage.content
                  )
                : cachedMessage.content;

            return (
              cachedCanonicalContent.trim() ===
              serverMessage.content.trim()
            );
          },
        );

      if (cachedIndex < 0) {
        return serverMessage;
      }

      usedCachedIndexes.add(cachedIndex);
      const cachedMessage =
        cachedHistory[cachedIndex];
      const canRestoreUserLabel =
        serverMessage.role === "user" &&
        cachedMessage.requestContent?.trim() ===
          serverMessage.content.trim();

      return {
        ...serverMessage,
        created_at: serverMessage.created_at || cachedMessage.created_at,
        content: canRestoreUserLabel
          ? cachedMessage.content
          : serverMessage.content,
        requestContent:
          cachedMessage.requestContent,
        quickReplies:
          serverMessage.quickReplies !== undefined
            ? serverMessage.quickReplies
            : cachedMessage.quickReplies,
        cards:
          serverMessage.cards !== undefined
            ? serverMessage.cards
            : cachedMessage.cards,
      };
    },
  );
}

/* =========================================================
   Local Storage
========================================================= */

const CHAT_HISTORY_KEY =
  "blink_chat_history";
type CachedChatHistory = { version: 1; expiresAt: number; messages: ChatMessage[]; }; const CHAT_HISTORY_TTL_MS = 30 * 60 * 1000;

const CHAT_ASSISTANT_ROUTE_KEY =
  "blink_chat_assistant_route";


const FACE_ZONE_VALUES =
  new Set([
  "t_zone",
  "left_cheek",
  "right_cheek",
  "chin",
    "T字部位（額頭、鼻子）",
    "左臉頰區（含左眼周）",
    "右臉頰區（含右眼周）",
    "下巴區（嘴周、人中、下巴）",
  ]);


function isNumericSeverityQuickReplies(quickReplies: QuickReply[]): boolean {
  return quickReplies.length === 5 && quickReplies.every(
    (reply, index) => reply.value === String(index + 1),
  );
}

function isFaceZoneQuickReplyGroup(
  quickReplies: QuickReply[],
): boolean {
  return (
    quickReplies.length > 1 &&
    quickReplies.every(
      (quickReply) =>
        FACE_ZONE_VALUES.has(
          quickReply.value,
        ),
    )
  );
}


/* =========================================================
   Assistant Role Label

   首頁入口先使用 flow 顯示；一般聊天則在 Root Agent
   回傳實際 route 後切換為真正處理該輪需求的小幫手。
========================================================= */

function getAssistantRoleLabel(
  route: string,
): string {
  switch (
    route
  ) {
    case "product_search":
      return "我是產品小幫手，負責找出最適合您的產品！";

    case "skin_record":
      return "我是膚況紀錄小幫手，負責記錄並追蹤您的肌膚變化！";

    case "skin_consult":
      return "我是膚況諮詢小幫手，負責解答您的肌膚與成分問題！";

    case "customer_quiz":
      return "我是消費人格小幫手，負責分析您的購物偏好！";

    default:
      return "我是 BLINK AI 小幫手，正在判斷最適合處理您需求的服務！";
  }
}


function getAssistantRouteStorageKey(
  userId: string,
  conversationId: string,
): string {
  return (
    CHAT_ASSISTANT_ROUTE_KEY +
    ":" +
    userId +
    ":" +
    conversationId
  );
}


function getStoredAssistantRoute(
  userId: string,
  conversationId: string,
): string {
  if (
    typeof window === "undefined"
  ) {
    return "";
  }

  return (
    sessionStorage.getItem(
      getAssistantRouteStorageKey(
        userId,
        conversationId,
      ),
    ) ?? ""
  );
}


function saveAssistantRoute(
  userId: string,
  conversationId: string,
  route: string,
): void {
  if (
    typeof window === "undefined" ||
    !route ||
    route === "clarify"
  ) {
    return;
  }

  sessionStorage.setItem(
    getAssistantRouteStorageKey(
      userId,
      conversationId,
    ),
    route,
  );
}


function inferAssistantRouteFromHistory(
  history: ChatMessage[],
): string {
  for (
    let index = history.length - 1;
    index >= 0;
    index -= 1
  ) {
    const message =
      history[index];

    if (
      message.role !== "user"
    ) {
      continue;
    }

    const content =
      message.content.trim();

    if (!content) {
      continue;
    }

    if (
      /消費(?:者)?人格|購物人格|人格測驗|重新測驗/.test(
        content,
      )
    ) {
      return "customer_quiz";
    }

    const inferredRoute =
      classifyLocally(
        content,
      );

    if (
      inferredRoute !== "clarify"
    ) {
      return inferredRoute;
    }
  }

  return "";
}


/* =========================================================
   Chat Page Content
========================================================= */

function ChatPageContent() {
  const searchParams =
    useSearchParams();


  const router =
    useRouter();


  /*
   * conversation_id：
   *
   * 同一個 user_id 底下的
   * 某一個獨立對話。
   *
   * 舊網址沒有 conversation_id 時，
   * 使用 default，保留舊資料相容性。
   */

  const newConversationRequested =
    searchParams.get("new") === "1";

  const conversationId = newConversationRequested
    ? ""
    : (searchParams.get("conversation_id") || "default").trim() || "default";

  const isDraftConversation =
    searchParams.get("draft") === "1";

  useEffect(() => {
    draftCommittedRef.current = !isDraftConversation;
  }, [conversationId, isDraftConversation]);

  useEffect(() => {
    window.dispatchEvent(new CustomEvent("blink-active-conversation", { detail: conversationId }));
  }, [conversationId]);


  /* =======================================================
     Current Flow
  ======================================================= */

  const requestedFlow =
    useMemo(() => {
      const value =
        searchParams.get(
          "flow",
        );


      return isChatFlow(
        value,
      )
        ? value
        : null;
    }, [
      searchParams,
    ]);


  const flow: ChatFlow =
    requestedFlow ??
    "skin_consult";


  const config =
    CHAT_FLOW_CONFIG[
      flow
    ];


  const [
    activeAssistantRoute,
    setActiveAssistantRoute,
  ] =
    useState<string>(
      requestedFlow ??
      "clarify",
    );


  useEffect(
    () => {
      setActiveAssistantRoute(
        requestedFlow ??
        "clarify",
      );
    },
    [
      requestedFlow,
    ],
  );


  /* =======================================================
     Quiz Mode

     mode=start
     → 第一次測驗

     mode=retake
     → 重新測驗
  ======================================================= */

  const quizMode =
    searchParams.get(
      "mode",
    );


  /* =======================================================
     Greeting
  ======================================================= */

  const initialGreeting =
    flow ===
    "customer_quiz"
      ? (
          quizMode ===
          "retake"
            ? (
                "嗨～我們重新做一次消費人格測驗吧！" +
                "我會一次問你一題，請依照平常購買保養品時最接近你的想法回答就可以了 ♡"
              )
            : (
                "嗨～接下來我們來做個簡單的消費人格測驗吧！" +
                "我會一次問你一題，請依照平常購買保養品時最接近你的想法回答就可以了 ♡"
              )
        )
      : config.greeting;


  const loadingMessage =
    flow ===
    "skin_record"
      ? "BLINK AI 思考中，正在整理你的膚況"
      : "BLINK AI 思考中，正在為你整理需求";


  /* =======================================================
     Chat State
  ======================================================= */

  const [
    messages,
    setMessages,
  ] =
    useState<
      ChatMessage[]
    >([
      {
        id:
          "initial-greeting",

        role:
          "assistant",

        content:
          initialGreeting,
      },
    ]);


  /*
   * 是否已完成聊天歷史讀取。
   *
   * 避免頁面一載入時，
   * initial greeting 把舊紀錄覆蓋掉。
   */

  const [
    historyLoaded,
    setHistoryLoaded,
  ] =
    useState(
      false,
    );


  /*
   * 紀錄目前畫面已完成載入的 conversation_id。
   *
   * 避免切換對話瞬間，
   * 把上一個對話的 messages
   * 寫入新的 sessionStorage key。
   */

  const [
    loadedConversationId,
    setLoadedConversationId,
  ] =
    useState(
      "",
    );


  const [
    input,
    setInput,
  ] =
    useState(
      "",
    );


  const [
    isLoading,
    setIsLoading,
  ] =
    useState(
      false,
    );

  const hasVisibleLoadingIndicator = messages.some(
    (message) => message.id === "loading-indicator",
  );
  const isGenerationActive = isLoading || hasVisibleLoadingIndicator;
  const [conversationLimit, setConversationLimit] =
    useState<ConversationLimitStatus | null>(null);
  const conversationBlocked = conversationLimit?.blocked === true;
  const [
    newConversationError,
    setNewConversationError,
  ] = useState("");
  const conversationInitializing =
    newConversationRequested && !newConversationError;


  const [
    quickReplySelections,
    setQuickReplySelections,
  ] =
    useState<
      Record<string, string[]>
    >({});


  /* =======================================================
     Customer Quiz State
  ======================================================= */

  /*
   * 是否顯示 CustomerQuiz。
   *
   * false：
   * 還沒開始 Quiz，
   * 或 Quiz 已經結束並開始下一個對話。
   *
   * true：
   * 正在 Quiz，
   * 或正在查看 Quiz 結果。
   */

  const [
    showCustomerQuiz,
    setShowCustomerQuiz,
  ] =
    useState(
      false,
    );


  /*
   * Quiz 是否已經完整完成：
   *
   * 1. 所有題目作答完成
   * 2. submitQuiz 成功
   * 3. 已經取得消費人格 result
   *
   * 只有這時候，
   * 使用者輸入下一個聊天問題，
   * 才會把 Quiz 收起來。
   */

  const [
    quizCompleted,
    setQuizCompleted,
  ] =
    useState(
      false,
    );


  /* =======================================================
     Chat User

     登入時：
     blink_user_id

     未登入時：
     blink_guest_user_id
  ======================================================= */

const [userId, setUserId] = useState(""); useEffect(() => { let cancelled = false; void resolveCurrentUserId().then((resolvedUserId) => { if (!cancelled) setUserId(resolvedUserId); }).catch(() => console.error("Unable to resolve chat identity")); return () => { cancelled = true; }; }, []);


  const chatHistoryKey =
    useMemo(
      () =>
        CHAT_HISTORY_KEY +
        ":" +
        userId +
        ":" +
        conversationId,
      [
        userId,
        conversationId,
      ],
    );


  const pendingChatKey = useMemo(
    () => getPendingChatKey(chatHistoryKey),
    [chatHistoryKey],
  );

  function beginChatRequest(
    message: string,
    displayMessage?: string,
    clientMessageId?: string,
  ): string | null {
    if (chatRequestInFlightRef.current) {
      return null;
    }

    const requestId = crypto.randomUUID();
    chatRequestInFlightRef.current = true;
    writePendingChatRequest(
      pendingChatKey,
      message,
      displayMessage,
      clientMessageId,
      requestId,
    );
    setIsLoading(true);
    return requestId;
  }

  function finishChatRequest(): void {
    chatRequestInFlightRef.current = false;
    clearPendingChatRequest(
      pendingChatKey,
    );
    setIsLoading(false);
  }

  const autoSent = useRef(false);
  const newConversationStarted = useRef(false);
  const newConversationInFlight = useRef(false);
  const chatRequestInFlightRef = useRef(false);
  const draftCommittedRef = useRef(false);
  const messageAreaRef = useRef<HTMLDivElement | null>(null);
  const activeGenerationControllerRef = useRef<AbortController | null>(null);
  const streamUpdateFrameRef = useRef<number | null>(null);
  const pendingStreamContentRef = useRef("");

  function cancelScheduledStreamUpdate(): void {
    if (streamUpdateFrameRef.current !== null) {
      window.cancelAnimationFrame(streamUpdateFrameRef.current);
      streamUpdateFrameRef.current = null;
    }
    pendingStreamContentRef.current = "";
  }

  function scheduleStreamingReply(content: string): void {
    pendingStreamContentRef.current = content;
    if (streamUpdateFrameRef.current !== null) {
      return;
    }

    streamUpdateFrameRef.current = window.requestAnimationFrame(() => {
      streamUpdateFrameRef.current = null;
      const nextContent = pendingStreamContentRef.current;
      setMessages((current) =>
        current.map((message) =>
          message.id === "loading-indicator"
            ? {
                ...message,
                content: nextContent,
                created_at: message.created_at ?? new Date().toISOString(),
              }
            : message,
        ),
      );
      window.requestAnimationFrame(() => {
        const area = messageAreaRef.current;
        if (area) {
          area.scrollTop = area.scrollHeight;
        }
      });
    });
  }

  useEffect(() => () => cancelScheduledStreamUpdate(), []);

  function beginGenerationControl(): AbortController {
    const controller = new AbortController();
    activeGenerationControllerRef.current = controller;
    return controller;
  }

  function finishGenerationControl(controller: AbortController): void {
    if (activeGenerationControllerRef.current !== controller) {
      return;
    }

    activeGenerationControllerRef.current = null;
  }

  function cancelGeneration(): void {
    const controller = activeGenerationControllerRef.current;
    if (!controller) {
      finalizeCancelledGeneration();
      finishChatRequest();
      return;
    }

    controller.abort();
  }

  function isGenerationAbort(
    error: unknown,
    controller: AbortController,
  ): boolean {
    return (
      controller.signal.aborted ||
      (error instanceof DOMException && error.name === "AbortError")
    );
  }

  function finalizeCancelledGeneration(): void {
    setMessages((current) =>
      current.flatMap((message) => {
        if (message.id !== "loading-indicator") {
          return [message];
        }

        const partialContent = message.content.trim();
        if (!partialContent) {
          return [];
        }

        return [
          {
            ...message,
            id: crypto.randomUUID(),
            content: partialContent + "\n\n（已取消生成）",
            created_at: new Date().toISOString(),
          },
        ];
      }),
    );
  }



  const initialMessage =
    searchParams.get(
      "message",
    );


  /* =======================================================
     Load Chat History

     GetZep：
     主要來源

     sessionStorage：
     備援來源
  ======================================================= */

  useEffect(() => {
    if (
      !newConversationRequested ||
      !userId ||
      newConversationStarted.current
    ) {
      return;
    }

    newConversationStarted.current = true;
    setNewConversationError("");

    void createConversation(userId)
      .then((conversation) => {
        const params = new URLSearchParams(searchParams.toString());
        params.delete("new");
        params.set("conversation_id", conversation.conversation_id);
        params.set("draft", "1");
        router.replace("/chat?" + params.toString());
      })
      .catch((error) => {
        newConversationStarted.current = false;
        console.error("Create homepage conversation failed:", error);
        setNewConversationError("無法建立新對話，請稍後重試。");
      });
  }, [newConversationRequested, router, searchParams, userId]);

  useEffect(() => {
    let cancelled =
      false;


    /*
     * conversation_id 改變時，
     * 先停止自動儲存並清空舊畫面。
     */

    setHistoryLoaded(
      false,
    );
    setConversationLimit(null);

    setLoadedConversationId(
      "",
    );

    setMessages([
      {
        id:
          "initial-greeting",

        role:
          "assistant",

        content:
          initialGreeting,
      },
    ]);

    setInput(
      "",
    );

    setQuickReplySelections(
      {},
    );

    setShowCustomerQuiz(
      false,
    );

    setQuizCompleted(
      false,
    );

    autoSent.current =
      false;


    async function loadHistory() {
      if (
        !userId || !conversationId || isDraftConversation
      ) {
        setLoadedConversationId(
          conversationId,
        );

        setHistoryLoaded(
          true,
        );

        return;
      }


      let cachedHistory:
        ChatMessage[] = [];


      try {
        const saved =
          sessionStorage.getItem(
            chatHistoryKey,
          );
const parsed = saved ? (JSON.parse(saved) as CachedChatHistory) : null; if (parsed && parsed.version === 1 && Number.isFinite(parsed.expiresAt) && parsed.expiresAt > Date.now() && Array.isArray(parsed.messages)) { cachedHistory = dedupeConsecutiveMessages(parsed.messages.map(normalizeInternalActionMessage).filter((message) => message.id !== "loading-indicator" && isVisibleChatMessage(message))); } else if (saved) { window.sessionStorage.removeItem(chatHistoryKey); }
      } catch (error) {
        console.error(
          "Load local chat history failed:",
          error,
        );
      }


      let restored:
        ChatMessage[] = [];


      try {
        const historyState = await getChatHistoryState(
          userId,
          conversationId,
        );
        setConversationLimit(historyState.conversation_limit);
        const remote =
          dedupeConsecutiveMessages(
            historyState.messages.map(
              normalizeInternalActionMessage,
            ).filter(
              (message) =>
                (
                  message.role === "user" ||
                  message.role === "assistant"
                ) &&
                isVisibleChatMessage(message),
            ),
          );

        /*
         * The server owns content, order and identity.
         * Local data may only restore presentation fields
         * that the history endpoint does not persist yet.
         */
        restored =
          mergeServerHistoryWithCachedPresentation(
            remote,
            cachedHistory,
          );
      } catch (error) {
        console.error(
          "Load GetZep history failed:",
          error,
        );

        /* Offline/network fallback only. */
        restored = cachedHistory;
      }

      if (
        !cancelled
      ) {
        const restoredRoute =
          requestedFlow ??
          getStoredAssistantRoute(
            userId,
            conversationId,
          );

        const inferredRoute =
          restoredRoute ||
          inferAssistantRouteFromHistory(
            restored,
          );

        setActiveAssistantRoute(
          inferredRoute ||
          "clarify",
        );

        if (inferredRoute) {
          saveAssistantRoute(
            userId,
            conversationId,
            inferredRoute,
          );
        }

        if (
          restored.length >
          0
        ) {
          /* Existing conversations must be rendered from persisted history only. */
          setMessages(restored);
        }


      setLoadedConversationId(conversationId);
        setHistoryLoaded(
          true,
        );
      }
    }


    void loadHistory();


    return () => {
      cancelled =
        true;
    };
  }, [
    chatHistoryKey,
    userId,
    conversationId,
    initialGreeting,
    requestedFlow,
    isDraftConversation,
  ]);

  useEffect(() => {
    const pending =
      readPendingChatRequest(pendingChatKey);
    if (!pending) return;

    let cancelled = false;
    let timer: number | null =
      null;

    const ensureLoadingIndicator = () => {
      if (cancelled) return;
      setIsLoading(true);
      setMessages((current) => {
        const next = [...current];
        const hasPendingUser =
          next.some(
            (message) =>
              message.role === "user" &&
              (
                (
                  pending.clientMessageId &&
                  message.id ===
                    pending.clientMessageId
                ) ||
                (
                  message.requestContent ??
                  message.content
                ).trim() ===
                  pending.message.trim()
              ),
          );

        if (
          pending.displayMessage &&
          !hasPendingUser
        ) {
          next.push({
            id:
              pending.clientMessageId ||
              `pending-user-${pending.startedAt}`,
            role: "user",
            content: pending.displayMessage,
            requestContent: pending.message,
          });
        }

        if (
          !next.some(
            (message) =>
              message.id ===
              "loading-indicator",
          )
        ) {
          next.push({
            id: "loading-indicator",
            role: "assistant",
            content: loadingMessage,
          });
        }

        return next;
      });
    };

    const stopRestoredLoading = () => {
      if (cancelled) return;
      clearPendingChatRequest(pendingChatKey);
      setIsLoading(false);
      setMessages((current) =>
        current.filter(
          (message) =>
            message.id !== "loading-indicator",
        ),
      );
    };

    let pollDelayMs = 1000;
  let missingJobStartedAt: number | null = null;

    async function pollForCompletedReply() {
      if (cancelled) return;

    if (!pending.requestId) {
      stopRestoredLoading();
      return;
    }

      try {

          const job = await getChatJobStatus(
            userId,
            pending.requestId,
          );
      if (job === null) {
        missingJobStartedAt ??= Date.now();
        if (Date.now() - missingJobStartedAt >= 15_000) {
          stopRestoredLoading();
          return;
        }
      } else {
        missingJobStartedAt = null;
      }


          if (job?.status === "completed" && job.result && !cancelled) {
            const res = job.result;
            setConversationLimit(res.conversation_limit ?? null);
            if (res.route) {
              setActiveAssistantRoute(res.route);
              saveAssistantRoute(userId, conversationId, res.route);
            }
            clearPendingChatRequest(pendingChatKey);
            setMessages((current) => [
              ...current.filter(
                (message) => message.id !== "loading-indicator",
              ),
              {
                id: crypto.randomUUID(),
                role: "assistant",
                content: res.reply,
                created_at: new Date().toISOString(),
                quickReplies: res.cards?.length ? [] : (res.quick_replies ?? []),
                cards: res.cards ?? [],
              },
            ]);
            setLoadedConversationId(conversationId);
            setHistoryLoaded(true);
            setIsLoading(false);
            window.dispatchEvent(
              new CustomEvent("blink-conversations-updated", {
                detail: { conversationId },
              }),
            );
            return;
          }

          if (job?.status === "failed" && !cancelled) {
            stopRestoredLoading();
            setMessages((current) => [
              ...current,
              {
                id: crypto.randomUUID(),
                role: "assistant",
                content: job.error || "這次回覆未完成，請再試一次。",
                created_at: new Date().toISOString(),
              },
            ]);
            return;
          }
      } catch (error) {
        console.error("Poll pending chat status failed:", error);
      }

      if (Date.now() - pending.startedAt >= CHAT_PENDING_TTL_MS) {
        stopRestoredLoading();
        return;
      }

      ensureLoadingIndicator();
      timer = window.setTimeout(pollForCompletedReply, pollDelayMs);
      pollDelayMs = Math.min(Math.round(pollDelayMs * 1.6), 5000);
    }

    ensureLoadingIndicator();
    void pollForCompletedReply();

    return () => {
      cancelled = true;
      if (timer !== null) {
        window.clearTimeout(timer);
      }
    };
  }, [
    conversationId,
    loadingMessage,
    pendingChatKey,
    requestedFlow,
    userId,
  ]);

  useEffect(() => {
    if (
      !historyLoaded ||
      loadedConversationId !== conversationId ||
      !messageAreaRef.current
    ) {
      return;
    }

    const scrollToLatest = () => {
      const area = messageAreaRef.current;
      if (area) area.scrollTop = area.scrollHeight;
    };

    const frame = window.requestAnimationFrame(scrollToLatest);
    const shortDelay = window.setTimeout(scrollToLatest, 120);
    const layoutDelay = window.setTimeout(scrollToLatest, 450);

    return () => {
      window.cancelAnimationFrame(frame);
      window.clearTimeout(shortDelay);
      window.clearTimeout(layoutDelay);
    };
  }, [conversationId, historyLoaded, loadedConversationId, messages.length]);


  /* =======================================================
     Save Chat History

     messages 每次改變，
     自動保存到 sessionStorage。
  ======================================================= */

  useEffect(() => {
    /*
     * 第一次聊天歷史還沒讀完之前，
     * 絕對不要寫入。
     *
     * 否則 initial greeting
     * 可能覆蓋原本聊天。
     */

    if (
      !historyLoaded ||
      !userId ||
      loadedConversationId !==
        conversationId
    ) {
      return;
    }


    try {
      const messagesToSave =
        messages.filter(
          (
            message,
          ) =>
            message.id !==
              "loading-indicator" &&
            isVisibleChatMessage(
              message,
            ),
        );


      sessionStorage.setItem(
        chatHistoryKey,
JSON.stringify({ version: 1, expiresAt: Date.now() + CHAT_HISTORY_TTL_MS, messages: messagesToSave } satisfies CachedChatHistory),
);
    } catch (
      error
    ) {
      console.error(
        "Save chat history failed:",
        error,
      );
    }
  }, [
    messages,
    historyLoaded,
    chatHistoryKey,
    userId,
    conversationId,
    loadedConversationId,
  ]);


  /* =======================================================
     Send Normal Message

     一般聊天：
     → sendRootMessage()
     → Root Agent
  ======================================================= */

  async function sendMessage(
    text: string,
    showUserMessage = true,
  ) {
    if (conversationBlocked) {
      return;
    }
    const clientMessageId =
      showUserMessage
        ? crypto.randomUUID()
        : undefined;

    const requestId = beginChatRequest(
      text,
      showUserMessage ? text : undefined,
      clientMessageId,
    );
    if (!requestId) {
      return;
    }

    setMessages(
      (current) => {
        const next = showUserMessage
          ? [...current]
          : current.filter(
              (message) =>
                message.id !==
                "initial-greeting",
            );

        if (showUserMessage) {
          next.push({
            id: clientMessageId!,
            role: "user",
            content: text,
            requestContent: text,
            created_at: new Date().toISOString(),
          });
        }

        next.push({
          id: "loading-indicator",
          role: "assistant",
          content: loadingMessage,
        });

        return next;
      },
    );

    const generationController = beginGenerationControl();

    try {
      let streamedReply = "";
      const res =
        await sendRootMessageStreamWhenAvailable({
          message:
            text,

          userId,

          conversationId,
        requestId,

          flow:
            requestedFlow ??
            undefined,
          signal: generationController.signal,
      }, (delta) => {
          streamedReply += delta;
          scheduleStreamingReply(streamedReply);
        });

      cancelScheduledStreamUpdate();

      setConversationLimit(res.conversation_limit ?? null);

      if (
        isDraftConversation &&
        !draftCommittedRef.current &&
        !isConversationBootstrapMessage(text)
      ) {
        draftCommittedRef.current = true;
        const params = new URLSearchParams(searchParams.toString());
        params.delete("draft");
        params.delete("new");
        window.history.replaceState(null, "", "/chat?" + params.toString());
        window.dispatchEvent(new CustomEvent("blink-conversations-updated", {
          detail: { conversationId, title: previewConversationTitle(text, requestedFlow) },
        }));
      }


      setActiveAssistantRoute(
        res.route,
      );

      saveAssistantRoute(
        userId,
        conversationId,
        res.route,
      );


      setMessages(
        (
          current,
        ) => [
          ...current.filter(
            (
              message,
            ) =>
              message.id !==
              "loading-indicator",
          ),

          {
            id:
              crypto.randomUUID(),

            role:
              "assistant",

            content:
              res.reply,

            created_at: new Date().toISOString(),

            quickReplies:
              res.cards?.length
                ? []
                : (
                    res.quick_replies ??
                    []
                  ),

            cards:
              res.cards ??
              [],
          },
        ],
      );
    } catch (
      error
    ) {
      if (isGenerationAbort(error, generationController)) {
        finalizeCancelledGeneration();
        return;
      }

      if (error instanceof ConversationLimitReachedError) {
        setConversationLimit(error.limit);
        setMessages((current) =>
          current.filter(
            (message) =>
              message.id !== "loading-indicator" &&
              message.id !== clientMessageId,
          ),
        );
        return;
      }

      const failureMessage = getChatFailureMessage(error);
      console.warn("Send message rejected:", failureMessage);


      setMessages(
        (
          current,
        ) => [
          ...current.filter(
            (
              message,
            ) =>
              message.id !==
              "loading-indicator",
          ),

          {
            id:
              crypto.randomUUID(),

            role:
              "assistant",

            content:
              failureMessage,
          },
        ],
      );
    } finally {
      cancelScheduledStreamUpdate();
      finishGenerationControl(generationController);
      finishChatRequest();
    }
  }


  /* =======================================================
     Quick Reply
  ======================================================= */

  async function sendQuickReply(
    value: string,
    displayMessage: string,
    clientMessageId: string,
    action?: string,
  ) {
    if (conversationBlocked) {
      return;
    }
    const requestId = beginChatRequest(
      value,
      displayMessage,
      clientMessageId,
    );
    if (!requestId) {
      return;
    }

    setMessages(
      (current) => [
        ...current,
        {
          id: clientMessageId,
          role: "user",
          content: displayMessage,
          requestContent: value,
          created_at: new Date().toISOString(),
        },
        {
          id: "loading-indicator",
          role: "assistant",
          content: loadingMessage,
        },
      ],
    );

    const generationController = beginGenerationControl();

    try {
      let streamedReply = "";
      const res =
        await sendRootMessageStreamWhenAvailable({
          message:
            value,

          userId,

          conversationId,
        requestId,

          flow:
            requestedFlow ??
            undefined,
          action,
          signal: generationController.signal,
      }, (delta) => {
          streamedReply += delta;
          scheduleStreamingReply(streamedReply);
        });

      cancelScheduledStreamUpdate();

      setConversationLimit(res.conversation_limit ?? null);

      if (
        isDraftConversation &&
        !draftCommittedRef.current &&
        !isConversationBootstrapMessage(value)
      ) {
        draftCommittedRef.current = true;
        const params = new URLSearchParams(searchParams.toString());
        params.delete("draft");
        params.delete("new");
        window.history.replaceState(null, "", "/chat?" + params.toString());
        window.dispatchEvent(new CustomEvent("blink-conversations-updated", {
          detail: { conversationId, title: previewConversationTitle(displayMessage, requestedFlow) },
        }));
      }


      setActiveAssistantRoute(
        res.route,
      );

      saveAssistantRoute(
        userId,
        conversationId,
        res.route,
      );


      setMessages(
        (
          current,
        ) => [
          ...current.filter(
            (
              message,
            ) =>
              message.id !==
              "loading-indicator",
          ),

          {
            id:
              crypto.randomUUID(),

            role:
              "assistant",

            content:
              res.reply,

            created_at: new Date().toISOString(),

            quickReplies:
              res.cards?.length
                ? []
                : (
                    res.quick_replies ??
                    []
                  ),

            cards:
              res.cards ??
              [],
          },
        ],
      );
    } catch (
      error
    ) {
      if (isGenerationAbort(error, generationController)) {
        finalizeCancelledGeneration();
        return;
      }

      if (error instanceof ConversationLimitReachedError) {
        setConversationLimit(error.limit);
        setMessages((current) =>
          current.filter(
            (message) =>
              message.id !== "loading-indicator" &&
              message.id !== clientMessageId,
          ),
        );
        return;
      }

      const failureMessage = getChatFailureMessage(error);
      console.warn("Quick reply rejected:", failureMessage);


      setMessages(
        (
          current,
        ) => [
          ...current.filter(
            (
              message,
            ) =>
              message.id !==
              "loading-indicator",
          ),

          {
            id:
              crypto.randomUUID(),

            role:
              "assistant",

            content:
              failureMessage,
          },
        ],
      );
    } finally {
      cancelScheduledStreamUpdate();
      finishGenerationControl(generationController);
      finishChatRequest();
    }
  }


  /* =======================================================
     Option
  ======================================================= */

  function handleRegenerate(): void {
    if (isLoading || conversationBlocked) {
      return;
    }

    const lastUserIndex = messages.findLastIndex(
      (message) => message.role === "user",
    );
    if (lastUserIndex < 0) {
      return;
    }

    const lastUserMessage = messages[lastUserIndex];
    const requestText = (
      lastUserMessage.requestContent ?? lastUserMessage.content
    ).trim();
    if (!requestText) {
      return;
    }

    setMessages((current) => current.slice(0, lastUserIndex + 1));
    void sendMessage(requestText, false);
  }

  function handleOption(
    label: string,
    value: string,
    action?: string,
  ) {
    if (
      isLoading ||
      conversationBlocked
    ) {
      return;
    }


    const opensSkinReport =
      value === "open_skin_report" ||
      value === "我要看膚況分析表" ||
      value === "我要看膚況分析報告";


    if (opensSkinReport) {
      router.push(
        "/skin-report",
      );

      return;
    }


    const clientMessageId =
      crypto.randomUUID();

    void sendQuickReply(
      value,
      label,
      clientMessageId,
      action,
    );
  }


  function toggleQuickReplySelection(
    messageId: string,
    value: string,
  ) {
    if (isLoading) {
      return;
    }


    setQuickReplySelections(
      (current) => {
        const selected =
          current[messageId] ??
          [];

        const next =
          selected.includes(value)
            ? selected.filter(
                (item) =>
                  item !== value,
              )
            : [
                ...selected,
                value,
              ];

        return {
          ...current,
          [messageId]: next,
        };
      },
    );
  }


  function submitQuickReplySelection(
    messageId: string,
    quickReplies: QuickReply[],
  ) {
    const selectedValues =
      quickReplySelections[
        messageId
      ] ?? [];

    const selectedReplies =
      quickReplies.filter(
        (quickReply) =>
          selectedValues.includes(
            quickReply.value,
          ),
      );

    if (
      isLoading ||
      selectedReplies.length === 0
    ) {
      return;
    }


    setQuickReplySelections(
      (current) => {
        const next = {
          ...current,
        };

        delete next[messageId];
        return next;
      },
    );


    handleOption(
      selectedReplies
        .map(
          (quickReply) =>
            quickReply.label,
        )
        .join("、"),
      selectedReplies
        .map(
          (quickReply) =>
            quickReply.value,
        )
        .join("、"),
      isFaceZoneQuickReplyGroup(quickReplies) ? "record_location" : undefined,
    );
  }


  /* =======================================================
     End Chat

     只有按「結束對話」才會：

     1. 清除後端 session
     2. 清除 sessionStorage 對話
     3. 清除畫面 messages
     4. 關閉 Quiz
  ======================================================= */

  async function handleEndChat() {
    if (
      !userId ||
      isLoading ||
      newConversationInFlight.current
    ) {
      return;
    }


    newConversationInFlight.current = true;
    setNewConversationError("");
    setIsLoading(
      true,
    );


    try {
      /*
       * 建立新的 conversation_id。
       *
       * 舊 conversation 不刪除，
       * 因此仍會保留在側邊欄。
       */

      const conversation =
        await createConversation(
          userId,
        );
    setConversationLimit(null);


      const params =
        new URLSearchParams();


      params.set(
        "flow",
        flow,
      );


      params.set(
        "conversation_id",
        conversation
          .conversation_id,
      );
      params.set("draft", "1");


      if (
        quizMode
      ) {
        params.set(
          "mode",
          quizMode,
        );
      }


      /*
       * 通知 AppShell：
       * 對話清單需要重新整理。
       */

      window.dispatchEvent(
        new Event(
          "blink-conversations-updated",
        ),
      );


      router.push(
        `/chat?${params.toString()}`,
      );

    } catch (
      error
    ) {
      console.error(
        "Create conversation failed:",
        error,
      );
      setNewConversationError("無法建立新對話，請稍後重試。");

    } finally {
      newConversationInFlight.current = false;
      setIsLoading(
        false,
      );
    }
  }


  /* =======================================================
     Flow / Mode 改變

     不清除歷史 messages。

     但：

     - 關閉目前 Quiz UI
     - 清除 Quiz completed state
     - 清空輸入框
  ======================================================= */

  useEffect(() => {
    if (
      !historyLoaded ||
      newConversationRequested ||
      !conversationId
    ) {
      return;
    }


    setInput(
      "",
    );


    setShowCustomerQuiz(
      false,
    );


    setQuizCompleted(
      false,
    );


    autoSent.current =
      false;
  }, [
    flow,
    quizMode,
    historyLoaded,
  ]);

  useEffect(() => {
    if (
      !historyLoaded ||
      flow !==
        "customer_quiz" ||
      !userId
    ) {
      return;
    }

    const savedDraft =
      readSession<unknown | null>(
        `blink-customer-quiz-draft:${userId}`,
        null,
      );

    if (
      savedDraft
    ) {
      setShowCustomerQuiz(
        true,
      );

      setQuizCompleted(
        false,
      );
    }
  }, [
    flow,
    historyLoaded,
    userId,
  ]);


  /* =======================================================
     Initial Message

     skin_record：
     保留原本自動進場。

     customer_quiz：
     不會自動送 Root。
  ======================================================= */

  useEffect(() => {
    /*
     * 必須先完成聊天歷史讀取。
     */

    if (
      !historyLoaded ||
      newConversationRequested ||
      !conversationId
    ) {
      return;
    }


    /*
     * Customer Quiz 完全由前端 Quiz UI 處理。
     */

    if (
      flow ===
      "customer_quiz"
    ) {
      return;
    }


    const entryMessage =
      initialMessage ??
      (
        flow ===
        "skin_record"
          ? "cycle_skin_tracking"
          : null
      );


    const isAutomaticSkinRecordStart =
      entryMessage === "cycle_skin_tracking";

    if (isAutomaticSkinRecordStart && !isDraftConversation) {
      return;
    }

    if (
      entryMessage &&
      !autoSent.current &&
      messages.length === 1 &&
      messages[0]?.id ===
        "initial-greeting"
    ) {
      autoSent.current =
        true;


      void sendMessage(
        entryMessage,

        entryMessage !==
          "cycle_skin_tracking",
      );
    }

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    flow,
    initialMessage,
    isDraftConversation,
    historyLoaded,
    messages,
    newConversationRequested,
    conversationId,
  ]);


  /* =======================================================
     Submit
  ======================================================= */

  async function handleSubmit(
    event:
      FormEvent<HTMLFormElement>,
  ) {
    event.preventDefault();


    const trimmed =
      input.trim();


    if (
      !trimmed ||
      isLoading ||
      conversationInitializing ||
      conversationBlocked
    ) {
      return;
    }


    setInput(
      "",
    );


    /* =====================================================
       Customer Quiz - 第一次進入

       使用者從 Profile：

       開始測驗
       或
       重新測驗

       進入 customer_quiz。

       第一個文字輸入只當作：

       「我要開始測驗」

       不送 Root Agent。
    ===================================================== */

    if (
      flow ===
        "customer_quiz" &&
      !showCustomerQuiz
    ) {
      setMessages(
        (
          current,
        ) => [
          ...current,

          {
            id:
              crypto.randomUUID(),

            role:
              "user",

            content:
              trimmed,
          },

          {
            id:
              crypto.randomUUID(),

            role:
              "assistant",

            content:
              "好～那我們開始吧 ♡",
          },
        ],
      );


      setShowCustomerQuiz(
        true,
      );


      setQuizCompleted(
        false,
      );


      return;
    }


    /* =====================================================
       Customer Quiz 已完成

       情況：

       使用者已經：

       1. 完成 Quiz
       2. 看完人格分析結果

       接著在下面輸入新的問題：

       「請推薦乾肌適合的產品給我」

       代表他已經開始下一段一般聊天。

       此時：

       1. CustomerQuiz 收起
       2. Quiz 結果仍然已存在 MongoDB
       3. 舊聊天紀錄不刪除
       4. 新問題正常送 Root Agent
    ===================================================== */

    if (
      flow ===
        "customer_quiz" &&
      showCustomerQuiz &&
      quizCompleted
    ) {
      setShowCustomerQuiz(
        false,
      );


      setQuizCompleted(
        false,
      );


      void sendMessage(
        trimmed,
      );


      return;
    }


    /* =====================================================
       Normal Chat

       其他 flow：
       正常送 Root Agent。

       Quiz 尚未完成時，
       如果使用者仍然輸入文字，
       也維持你目前原本的行為。
    ===================================================== */

    void sendMessage(
      trimmed,
    );
  }


  /* =======================================================
     Render
  ======================================================= */

  return (
    <main className="pure-chat-page">

      <section className="retro-chat-panel">

        {/* =========================
            Header
        ========================== */}

        <header className="chat-panel-header">

          <div className="chat-assistant">

            <div
              className="chat-avatar"
              aria-hidden="true"
            >
<img src="/blink-ai-avatar.svg" alt="" />
</div>


            <div>

              <h1>
                {config.assistantName}
              </h1>


              <p className="assistant-role-status">
                {getAssistantRoleLabel(
                  activeAssistantRoute,
                )}
              </p>

            </div>

          </div>


          <button
            type="button"
            className="end-chat-button"
            disabled={isLoading}
            aria-busy={isLoading}
            onClick={() =>
              void handleEndChat()
            }
          >
            ＋ 新對話
          </button>
          {newConversationError && (
            <p className="chat-navigation-error" role="alert">{newConversationError}</p>
          )}

        </header>


        {/* =========================
            Message Area
        ========================== */}

        <div className="chat-message-area" ref={messageAreaRef}>

          {messages
          .filter(
            (message) =>
              !(
                message.id === "initial-greeting" &&
                flow === "skin_record" &&
                (newConversationRequested ||
                  isDraftConversation ||
                  autoSent.current)
              ),
          )
          .map((message) =>
            flow === "skin_record" &&
            message.role === "assistant" &&
            message.content.trim() === SKIN_RECORD_ENTRY_PROMPT &&
            !(message.quickReplies && message.quickReplies.length)
              ? {
                  ...message,
                  quickReplies: SKIN_RECORD_QUICK_REPLIES,
                }
              : message,
          )
          .map(
            (
              message,
              messageIndex,
            ) => (

              <Fragment key={message.id}>

              {message.created_at &&
                shouldShowDateDivider(
                  messages,
                  messageIndex,
                ) && (
                  <div
                    className="chat-date-divider"
                    role="separator"
                    aria-label={formatMessageDateLabel(message.created_at)}
                  >
                    <span>
                      {formatMessageDateLabel(message.created_at)}
                    </span>
                  </div>
                )}

              <div
                className={
                  `chat-row chat-row--${message.role}`
                }
              >

                {message.role ===
                  "assistant" && (

                  <div
                    className="message-avatar"
                    aria-hidden="true"
                  >
<img src="/blink-ai-avatar.svg" alt="" />
</div>

                )}


                <div
                  className={
                    `chat-bubble chat-bubble--${message.role}`
                  }
                >

                  {message.id ===
                  "loading-indicator" ? (
                    <span
                      className="chat-thinking-status"
                      role="status"
                      aria-live="polite"
                    >
                      {message.content.replace(
                        /\.{3}$/,
                        "",
                      )}
                      <span
                        className="chat-thinking-dots"
                        aria-hidden="true"
                      >
                        <span />
                        <span />
                        <span />
                      </span>
                    </span>
                  ) : message.role === "assistant" ? (
                    <ReadableAssistantMessage content={message.content} />
                  ) : (
                    renderMessageContent(message.content)
                  )}


                  {message.created_at && !Number.isNaN(Date.parse(message.created_at)) && (
                    <time className="chat-message-time" dateTime={message.created_at}>
                      {formatMessageTime(message.created_at)}
                    </time>
                  )}

                  {message.role === "assistant" &&
                messages.some((item) => item.role === "user") &&
                messageIndex ===
                  messages.findLastIndex((item) => item.role === "assistant") ? (
                <div className="chat-message-actions" aria-label="AI 回覆操作">
                  <button
                    type="button"
                    className="chat-message-action chat-message-action--regenerate"
                    onClick={handleRegenerate}
                    disabled={isGenerationActive || conversationBlocked}
                    aria-label="重新生成"
                    title="重新生成"
                  >
                    <svg
                      viewBox="0 0 24 24"
                      aria-hidden="true"
                      focusable="false"
                    >
                      <path d="M20 7h-4V3" />
                      <path d="M20 7a8 8 0 0 0-13.7-2.8L4 7" />
                      <path d="M4 17h4v4" />
                      <path d="M4 17a8 8 0 0 0 13.7 2.8L20 17" />
                    </svg>
                  </button>
                  {isGenerationActive ? (
                    <button
                      type="button"
                      className="chat-message-action chat-message-action--cancel"
                      onClick={cancelGeneration}
                      aria-label="取消生成"
                      title="取消生成"
                    >
                      <svg
                        viewBox="0 0 24 24"
                        aria-hidden="true"
                        focusable="false"
                      >
                        <path d="M6 6l12 12M18 6 6 18" />
                      </svg>
                    </button>
                  ) : null}
                </div>
              ) : null}

              {/* =========================
                      Tracking Cards
                  ========================== */}

                  {message.role ===
                    "assistant" &&
                    message.cards &&
                    message.cards.length >
                      0 &&
        !isNumericSeverityQuickReplies(message.quickReplies) && (

                    <div
                      className="tracking-card-stack"
                      aria-label="膚況互動字卡"
                    >

                      {message.cards.map(
                        (
                          card,
                          cardIndex,
                        ) => (

                          <section
                            className={
                              `tracking-card tracking-card-${card.type}`
                            }
                            key={
                              `${card.type}-${card.title}-${cardIndex}`
                            }
                          >

                            <span className="tracking-card-kicker">

                              {card.type ===
                              "choice"
                                ? "CHOOSE"
                                : "REVIEW"}

                            </span>


                            <h3>
                              {card.title}
                            </h3>


                            {card.type ===
                            "choice" ? (

                              <div className="tracking-card-options">

                                {card.options.map(
                                  (
                                    option,
                                  ) => (

                                    <button
                                      key={
                                        `${option.label}-${option.value}`
                                      }
                                      type="button"
                                      disabled={
                                        isLoading
                                      }
                                      onClick={() =>
                                        handleOption(
                                          option.label,
                                          option.value,
                                        )
                                      }
                                    >
                                      {option.label}
                                    </button>

                                  ),
                                )}

                              </div>

                            ) : (

                              <>

                                <dl className="tracking-summary-fields">

                                  {card.fields.map(
                                    (
                                      field,
                                    ) => (

                                      <div
                                        key={
                                          `${field.label}-${field.value}`
                                        }
                                      >

                                        <dt>
                                          {field.label}
                                        </dt>

                                        <dd>
                                          {field.value}
                                        </dd>

                                      </div>

                                    ),
                                  )}

                                </dl>


                                <div className="tracking-card-actions">

                                  {card.actions.map(
                                    (
                                      action,
                                    ) => (

                                      <button
                                        key={
                                          `${action.label}-${action.value}`
                                        }
                                        type="button"
                                        className={
                                          action.value ===
                                          "確認"
                                            ? "is-primary"
                                            : ""
                                        }
                                        disabled={
                                          isLoading
                                        }
                                        onClick={() =>
                                          handleOption(
                                            action.label,
                                            action.value,
                                          )
                                        }
                                      >
                                        {action.label}
                                      </button>

                                    ),
                                  )}

                                </div>

                              </>

                            )}

                          </section>

                        ),
                      )}

                    </div>

                  )}


                  {/* =========================
                      Quick Replies
                  ========================== */}

                  {message.role ===
                    "assistant" &&
                    message.quickReplies &&
        message.quickReplies.length >
          0 &&
        (

                    message.quickReplies
                      .map(
                        (
                          quickReply,
                        ) =>
                          quickReply.label,
                      )
                      .join(",") ===
                    "1,2,3,4,5" ? (

                      <SeveritySlider
                        disabled={
                          isLoading
                        }

                        onSubmit={(
                          percentage,
                        ) => {

                          const severity =
                            Math.min(
                              5,
                              Math.max(
                                1,
                                Math.ceil(
                                  percentage /
                                    20,
                                ),
                              ),
                            );


                          const clientMessageId =
                            crypto.randomUUID();
                          const displayMessage =
                            `嚴重程度 ${percentage}%`;
                          const requestContent =
                            String(severity);

                          void sendQuickReply(
                            requestContent,
                            displayMessage,
                            clientMessageId,
                          );
                        }}
                      />

                    ) : isFaceZoneQuickReplyGroup(
                      message.quickReplies,
                    ) ? (

                      <div
                        className="quick-replies quick-replies--multi"
                      >

                        <p className="quick-replies__hint">
                          請直接點選下方固定部位，可以複選
                        </p>


                        {uniqueQuickReplies(message.quickReplies).map(
                          (quickReply) => {
                            const selected =
                              (
                                quickReplySelections[
                                  message.id
                                ] ?? []
                              ).includes(
                                quickReply.value,
                              );

                            return (
                              <button
                                key={
                                  quickReply.value
                                }
                                type="button"
                                className={
                                  selected
                                    ? "quick-reply-option is-selected"
                                    : "quick-reply-option"
                                }
                                aria-pressed={
                                  selected
                                }
                                disabled={
                                  isLoading
                                }
                                onClick={() =>
                                  toggleQuickReplySelection(
                                    message.id,
                                    quickReply.value,
                                  )
                                }
                              >
                                {quickReply.label}
                              </button>
                            );
                          },
                        )}


                        <button
                          type="button"
                          className="quick-reply-submit"
                          disabled={
                            isLoading ||
                            (
                              quickReplySelections[
                                message.id
                              ] ?? []
                            ).length === 0
                          }
                          onClick={() =>
                            submitQuickReplySelection(
                              message.id,
                              message.quickReplies ?? [],
                            )
                          }
                        >
                          就記這些部位（{
                            (
                              quickReplySelections[
                                message.id
                              ] ?? []
                            ).length
                          }）
                        </button>

                      </div>

                    ) : (

                      <div
                        className="quick-replies"
                        style={{
                          display:
                            "flex",

                          flexWrap:
                            "wrap",

                          gap:
                            "8px",

                          marginTop:
                            "12px",
                        }}
                      >

                        {uniqueQuickReplies(message.quickReplies).map(
                          (
                            quickReply,
                          ) => (

                            <button
                              key={
                                quickReply.value
                              }
                              type="button"
                              disabled={
                                isLoading
                              }
                              onClick={() =>
                                handleOption(
                                  quickReply.label,
                                  quickReply.value,
                    quickReply.action,
                                )
                              }
                              style={{
                                padding:
                                  "6px 14px",

                                borderRadius:
                                  "16px",

                                border:
                                  "1px solid #b48ead",

                                background:
                                  "#fff",

                                color:
                                  "#6b4f7a",

                                cursor:
                                  "pointer",

                                fontSize:
                                  "14px",
                              }}
                            >
                              {isNumericSeverityQuickReplies(message.quickReplies) && /^[1-5]｜/.test(quickReply.label) ? (<><span style={{ color: Number(quickReply.value) <= 2 ? "#B8860B" : Number(quickReply.value) === 3 ? "#C65B14" : "#B3261E" }}>{quickReply.value}</span>{quickReply.label.slice(1)}</>) : quickReply.label}
                            </button>

                          ),
                        )}

                      </div>

                    )
                  )}

                </div>

              </div>

              </Fragment>

            ),
          )}


          {/* =========================
              Customer Quiz

              Quiz 完成後不會立刻消失。

              等使用者開始下一個正常對話時，
              才由 handleSubmit 關閉。
          ========================== */}

          {flow ===
            "customer_quiz" &&
          showCustomerQuiz ? (

            <CustomerQuiz
              onCompletedChange={
                setQuizCompleted
              }
            />

          ) : null}

        </div>


        {/* =========================
            Input Form
        ========================== */}

        <form
          className="chat-input-form"
          onSubmit={
            handleSubmit
          }
        >

          <textarea
            onKeyDown={(
              event,
            ) => {

              if (
                event.key ===
                  "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent
                  .isComposing
              ) {
                event.preventDefault();


                event.currentTarget
                  .form
                  ?.requestSubmit();
              }

            }}

            value={
              input
            }

            onChange={(
              event,
            ) =>
              setInput(
                event.target.value,
              )
            }

            disabled={
              isLoading ||
              conversationInitializing ||
              conversationBlocked
            }
            placeholder={
              conversationInitializing
                ? "正在建立新對話…"
                : conversationBlocked
                  ? "此對話已額滿，請開新對話"
                : flow ===
                    "customer_quiz" &&
                  !showCustomerQuiz
                  ? "回覆後開始測驗..."
                  : "請輸入您的需求"
            }

            rows={1}
          />


          <button
          type={isGenerationActive ? "button" : "submit"}
          className={
            isGenerationActive
              ? "chat-send-button chat-send-button--generation"
              : "chat-send-button"
          }
          aria-label={
            isGenerationActive
              ? "停止生成"
              : "傳送訊息"
          }
          title={
            isGenerationActive
              ? "停止生成"
              : "傳送訊息"
          }
          onClick={
            isGenerationActive
              ? (event) => {
                  event.preventDefault();
                  cancelGeneration();
                }
              : undefined
          }
          disabled={
            conversationInitializing ||
            conversationBlocked ||
            (!isGenerationActive && !input.trim())
          }
        >
          {isGenerationActive ? (
            <span className="chat-send-stop-icon" aria-hidden="true" />
          ) : (
            "➤"
          )}
        </button>

        </form>

      </section>

    </main>
  );
}


/* =========================================================
   Page
========================================================= */

export default function ChatPage() {
  return (
    <Suspense
      fallback={

        <main className="pure-chat-page">

          <section className="retro-chat-panel">

            <div className="chat-message-area">
              BLINK AI 聊天載入中…
            </div>

          </section>

        </main>

      }
    >

      <ChatPageContent />

    </Suspense>
  );
}


/* =========================================================
   Severity Slider
========================================================= */

function SeveritySlider({
  onSubmit,
  disabled,
}: {
  onSubmit:
    (
      percentage:
        number,
    ) => void;

  disabled:
    boolean;
}) {
  const [
    val,
    setVal,
  ] =
    useState(
      50,
    );


  const [
    sent,
    setSent,
  ] =
    useState(
      false,
    );


  return (
    <div
      style={{
        marginTop:
          "12px",

        maxWidth:
          "320px",
      }}
    >

      <div
        style={{
          display:
            "flex",

          justifyContent:
            "space-between",

          fontSize:
            "12px",

          color:
            "#6b4f7a",
        }}
      >

        <span>
          1
        </span>

        <span>
          50
        </span>

        <span>
          100
        </span>

      </div>


      <input
        type="range"
        min={1}
        max={100}
        value={
          val
        }
        disabled={
          disabled ||
          sent
        }
        onChange={(
          event,
        ) =>
          setVal(
            Number(
              event.target.value,
            ),
          )
        }
        style={{
          width:
            "100%",

          height:
            "6px",

          borderRadius:
            "3px",

          appearance:
            "none",

          outline:
            "none",

          background:
            `linear-gradient(
              to right,
              #b8e6d5 0%,
              #7fd4b8 ${val}%,
              #e8e8e8 ${val}%,
              #e8e8e8 100%
            )`,

          accentColor:
            "#7fd4b8",

          cursor:
            sent
              ? "default"
              : "pointer",
        }}
      />


      <div
        style={{
          display:
            "flex",

          alignItems:
            "center",

          justifyContent:
            "space-between",

          marginTop:
            "6px",
        }}
      >

        <span
          style={{
            fontSize:
              "13px",

            color:
              "#6b4f7a",
          }}
        >
          嚴重程度：
          {val}%
        </span>


        <button
          type="button"
          disabled={
            disabled ||
            sent
          }
          onClick={() => {
            setSent(
              true,
            );


            onSubmit(
              val,
            );
          }}
          style={{
            padding:
              "4px 14px",

            borderRadius:
              "14px",

            border:
              "1px solid #7fd4b8",

            background:
              sent
                ? "#e8e8e8"
                : "#fff",

            color:
              "#3d8b70",

            fontSize:
              "13px",

            cursor:
              sent
                ? "default"
                : "pointer",
          }}
        >

          {sent
            ? "已送出"
            : "確定"}

        </button>

      </div>

    </div>
  );
}
