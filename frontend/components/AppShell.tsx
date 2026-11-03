"use client";

import Link from "next/link";

import {
  usePathname,
  useRouter,
} from "next/navigation";

import type {
  MouseEvent as ReactMouseEvent,
  ReactNode,
} from "react";

import {
  useEffect,
  useRef,
  useState,
} from "react";

import {
  Icon,
  type IconName,
} from "./Icon";

import {
  createConversation,
  deleteConversation,
  listConversations,
  renameConversation,
  type ConversationSummary,
} from "@/lib/api";

import {  getCurrentAuthIdentity,  logoutCurrentSession,  subscribeToLogout,} from "@/lib/auth-client";

/* =========================================================
Navigation Types
========================================================= */

type NavItem = {
  href: string;
  label: string;
  icon: IconName;
  external?: boolean;
};

type ConversationContextMenu = {
  conversationId: string;
  conversationTitle: string;
  x: number;
  y: number;
};

/* =========================================================
Navigation
========================================================= */

const navItems: NavItem[] = [
  { href: "/", label: "首頁", icon: "home" },
  { href: "/skin-photo", label: "照片膚況分析", icon: "search" },
  {
    href:
      "/daily-checkin",

    label:
      "每日簽到",

    icon:
      "calendar",
  },

  {
    href:
      "/skin-report",

    label:
      "膚況紀錄",

    icon:
      "chart",
  },

  {
    href:
      "/knowledge-map",

    label:
      "美妝知識地圖",

    icon:
      "search",
  },

  {
    href:
      "/tomods",

    label:
      "三友藥妝專區",

    icon:
      "store",
  },
];

function SidebarImageIcon({
  name,
}: {
  name: IconName;
}) {
  return <Icon name={name} />;
}

/* =========================================================
Provider Display
========================================================= */

function getProviderLabel(
  provider: string,
): string {
  switch (
    provider
  ) {
    case "line":
      return "LINE 登入";

    case "google":
      return "Google 登入";

    case "email":
      return "BLINK 帳號";

    default:
      return "已登入";
  }
}

/* =========================================================
Conversation Title
========================================================= */

function getConversationTitle(
  conversation:
    ConversationSummary,
): string {
  const title =
    (
      conversation.title ||
      ""
    ).trim();

  /*
   * 舊資料可能把後端
   * 內部控制指令當成標題。
   */
  if (
    !title ||
    /^(cycle_|confirm_|skip$)/i
      .test(
        title,
      )
  ) {
    return "新對話";
  }

  return title;
}

function sortConversationsNewestFirst(
  conversations: ConversationSummary[],
): ConversationSummary[] {
  return [...conversations].sort((left, right) => {
    const leftTime = Date.parse(left.updated_at || left.created_at || "") || 0;
    const rightTime = Date.parse(right.updated_at || right.created_at || "") || 0;
    return rightTime - leftTime;
  });
}

function formatConversationTime(value?: string): string {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return "時間未記錄";
  return date.toLocaleString("zh-TW", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/* =========================================================
App Shell
========================================================= */

export function AppShell({
  children,
}: {
  children: ReactNode;
}) {
  const pathname =
    usePathname();

  const router =
    useRouter();

  /* =======================================================
  Login State
  ======================================================= */

  const [
    isLoggedIn,
    setIsLoggedIn,
  ] =
    useState(
      false,
    );

  const [authReady, setAuthReady] = useState(false);

  const [
    displayName,
    setDisplayName,
  ] =
    useState(
      "",
    );
const [currentUserId, setCurrentUserId] = useState("");

  const [
    authProvider,
    setAuthProvider,
  ] =
    useState(
      "",
    );

  /* =======================================================
  Conversation State
  ======================================================= */

  const [
    conversationOpen,
    setConversationOpen,
  ] =
    useState(
      false,
    );

  const [
    conversations,
    setConversations,
  ] =
    useState<
      ConversationSummary[]
    >(
      [],
    );

  const [
    conversationLoading,
    setConversationLoading,
  ] =
    useState(
      false,
    );

  const [
    conversationError,
    setConversationError,
  ] = useState("");

  const [
    conversationRetryAction,
    setConversationRetryAction,
  ] = useState<"load" | "create" | null>(null);

  const [
    creatingConversation,
    setCreatingConversation,
  ] = useState(false);

  const conversationLoadInFlight = useRef(false);

  const conversationCreateInFlight = useRef(false);
  const draftConversationsRef = useRef<Map<string, ConversationSummary>>(
    new Map(),
  );
  const [conversationTotal, setConversationTotal] = useState(0);
  const [conversationMax, setConversationMax] = useState(100);
  const [conversationHasMore, setConversationHasMore] = useState(false);
  const [conversationLoadingMore, setConversationLoadingMore] = useState(false);
  const conversationServerOffset = useRef(0);

  const [
    activeConversationId,
    setActiveConversationId,
  ] =
    useState(
      "default",
    );

  const [
    conversationContextMenu,
    setConversationContextMenu,
  ] =
    useState<
      ConversationContextMenu
      | null
    >(
      null,
    );

  const [
    deletingConversationId,
    setDeletingConversationId,
  ] =
    useState(
      "",
    );

  const [
    mobileSidebarOpen,
    setMobileSidebarOpen,
  ] =
    useState(
      false,
    );

  const [
    accountMenuOpen,
    setAccountMenuOpen,
  ] =
    useState(
      false,
    );

  const mobileMenuButtonRef =
    useRef<HTMLButtonElement | null>(null);
  const mobileSidebarCloseRef =
    useRef<HTMLButtonElement | null>(null);
  const mobileSidebarWasOpen =
    useRef(false);

  /* =======================================================
  Refresh Login State
  ======================================================= */

  async function refreshAuthState(options?: { force?: boolean }) {
    try {
      const identity = await getCurrentAuthIdentity(options);
      setCurrentUserId(identity?.userId ?? "");
      setIsLoggedIn(Boolean(identity?.userId));
      setDisplayName(identity?.displayName ?? "");
      setAuthProvider(identity?.provider ?? "");
    } catch (error) {
      /*
       * A timeout, deploy or temporary API outage does not prove that the
       * cookie session ended. Keep the last known UI state; protected APIs
       * remain the authority and will still reject an invalid session.
       * Logging this as an error makes the Next.js dev overlay cover the UI.
       */
      if (process.env.NODE_ENV !== "production") {
        console.warn(
          "Authentication state is temporarily unavailable",
          error instanceof Error ? error.message : "unknown error",
        );
      }
    } finally {
      setAuthReady(true);
    }
  }

  /* =======================================================
  Pathname Change

   /login
   ↓
   LINE / Google Login
   ↓
   /login/...-success
   ↓
   /
   ↓
   重新讀登入身份

  ======================================================= */

  useEffect(() => {
    void refreshAuthState();

    /*
     * 換頁後自動收起手機版側邊欄，
     * 避免新頁面仍被選單與遮罩蓋住。
     */
    setMobileSidebarOpen(
      false,
    );

    setAccountMenuOpen(
      false,
    );

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    pathname,
  ]);

  useEffect(() => {
    const viewport = window.visualViewport;
    const root = document.documentElement;
    let frameId: number | null = null;

    function updateViewportMetrics() {
      const height = viewport?.height ?? window.innerHeight;
      const offsetTop = viewport?.offsetTop ?? 0;

      root.style.setProperty(
        "--mobile-viewport-height",
        `${Math.round(height)}px`,
      );
      root.style.setProperty(
        "--mobile-viewport-offset-top",
        `${Math.round(offsetTop)}px`,
      );
    }

    function scheduleViewportUpdate() {
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId);
      }
      frameId = window.requestAnimationFrame(() => {
        frameId = null;
        updateViewportMetrics();
      });
    }

    updateViewportMetrics();
    viewport?.addEventListener("resize", scheduleViewportUpdate);
    viewport?.addEventListener("scroll", scheduleViewportUpdate, {
      passive: true,
    });
    window.addEventListener("resize", scheduleViewportUpdate);
    window.addEventListener("orientationchange", scheduleViewportUpdate);

    return () => {
      if (frameId !== null) {
        window.cancelAnimationFrame(frameId);
      }
      viewport?.removeEventListener("resize", scheduleViewportUpdate);
      viewport?.removeEventListener("scroll", scheduleViewportUpdate);
      window.removeEventListener("resize", scheduleViewportUpdate);
      window.removeEventListener("orientationchange", scheduleViewportUpdate);
      root.style.removeProperty("--mobile-viewport-height");
      root.style.removeProperty("--mobile-viewport-offset-top");
    };
  }, []);

  /* =======================================================
  Mobile Sidebar
  ======================================================= */

  useEffect(() => {
    if (
      !mobileSidebarOpen
    ) {
      if (mobileSidebarWasOpen.current) {
        mobileSidebarWasOpen.current = false;
        mobileMenuButtonRef.current?.focus();
      }
      return;
    }

    mobileSidebarWasOpen.current = true;
    const focusFrame = window.requestAnimationFrame(() => {
      mobileSidebarCloseRef.current?.focus();
    });

    const previousOverflow =
      document.body.style
        .overflow;

    document.body.style
      .overflow = "hidden";

    function handleKeyDown(
      event: KeyboardEvent,
    ) {
      if (
        event.key ===
        "Escape"
      ) {
        setMobileSidebarOpen(
          false,
        );
      }
    }

    function handleResize() {
      if (
        window.innerWidth >
        790
      ) {
        setMobileSidebarOpen(
          false,
        );
      }
    }

    window.addEventListener(
      "keydown",
      handleKeyDown,
    );

    window.addEventListener(
      "resize",
      handleResize,
    );

    return () => {
      window.cancelAnimationFrame(focusFrame);
      document.body.style
        .overflow =
        previousOverflow;

      window.removeEventListener(
        "keydown",
        handleKeyDown,
      );

      window.removeEventListener(
        "resize",
        handleResize,
      );
    };
  }, [
    mobileSidebarOpen,
  ]);

  /* =======================================================
  Auth Updated Event

   line-success/page.tsx
   google-success/page.tsx
   Profile save

   都可以 dispatch：

   blink-auth-updated

  ======================================================= */

  useEffect(() => {
    function handleAuthUpdated() {
      void refreshAuthState({ force: true });
    }
const unsubscribeFromLogout = subscribeToLogout(() => { setCurrentUserId(""); setIsLoggedIn(false); setDisplayName(""); setAuthProvider(""); setConversations([]); router.replace("/"); });

    /*
     * 同一個頁面內更新。
     */
    window.addEventListener(
      "blink-auth-updated",
      handleAuthUpdated,
    );

    /*
     * 不同 Browser Tab
     * 修改 localStorage 時更新。
     */
    window.addEventListener(
      "storage",
      handleAuthUpdated,
    );

    return () => {
      window.removeEventListener(
        "blink-auth-updated",
        handleAuthUpdated,
      );

      window.removeEventListener(
        "storage",
        handleAuthUpdated,
      );
unsubscribeFromLogout();
    };

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* =======================================================
  Logout
  ======================================================= */

async function handleLogout() {    try {      await logoutCurrentSession();    } catch {      console.error("Logout request failed");      return;    }    draftConversationsRef.current.clear();    setCurrentUserId("");    setIsLoggedIn(false);    setDisplayName("");    setAuthProvider("");    setConversationOpen(false);    setConversations([]);    setConversationContextMenu(null);    setMobileSidebarOpen(false);    router.push("/");  }

  /* =======================================================
  Current User ID
  ======================================================= */

  function getCurrentUserId():
    string {
    const identity =
      ({ userId: currentUserId });

    return (
      identity.userId
    );
  }

  /* =======================================================
  Conversation Sidebar
  ======================================================= */

  async function loadConversations({
    append = false,
    force = false,
  }: { append?: boolean; force?: boolean } = {}) {
    if (conversationLoadInFlight.current) return;

    conversationLoadInFlight.current = true;
    setConversationError("");
    setConversationRetryAction(null);

    const userId = getCurrentUserId();
    if (!userId) {
      setConversations([]);
      setConversationTotal(0);
      conversationServerOffset.current = 0;
      conversationLoadInFlight.current = false;
      return;
    }

    if (append) setConversationLoadingMore(true);
    else setConversationLoading(true);

    try {
      const offset = append ? conversationServerOffset.current : 0;
      const page = await listConversations(userId, {
        limit: 20,
        offset,
        force,
      });
      const hydrated = page.conversations;
      const serverConversationIds = new Set(
        hydrated.map((conversation) => conversation.conversation_id),
      );
      serverConversationIds.forEach((conversationId) => {
        draftConversationsRef.current.delete(conversationId);
      });
      const localDrafts = Array.from(draftConversationsRef.current.values())
        .filter((conversation) => !serverConversationIds.has(conversation.conversation_id));

      setConversations((current) => {
        const merged = append
          ? [...current, ...hydrated]
          : [...localDrafts, ...hydrated];
        const unique = new Map(
          merged.map((conversation) => [conversation.conversation_id, conversation]),
        );
        return sortConversationsNewestFirst(Array.from(unique.values()));
      });
      conversationServerOffset.current = page.offset + hydrated.length;
      setConversationTotal(page.total);
      setConversationMax(page.maxConversations);
      setConversationHasMore(page.hasMore);
    } catch (error) {
      console.error("Load conversations failed:", error);
      setConversationError("無法載入對話紀錄，請稍後重試。");
      setConversationRetryAction("load");
    } finally {
      setConversationLoading(false);
      setConversationLoadingMore(false);
      conversationLoadInFlight.current = false;
    }
  }

  async function toggleConversationPanel() {
    const nextOpen =
      !conversationOpen;

    setConversationOpen(
      nextOpen,
    );

    if (
      nextOpen
    ) {
      const userId =
        getCurrentUserId();

      if (
        !userId
      ) {
        setConversationOpen(
          false,
        );

        router.push(
          "/login",
        );

        return;
      }

      await loadConversations({ force: conversations.length === 0 });
    }
  }

  async function startNewConversation() {
    if (conversationCreateInFlight.current) {
      return;
    }

    const userId = getCurrentUserId();

    if (conversationTotal >= conversationMax) {
      setConversationError(
        "此帳號已達 " + conversationMax + " 筆對話上限，請先刪除不需要的對話。",
      );
      return;
    }

    if (!userId) {
      setConversationError("請先登入後再建立新對話。");
      setConversationRetryAction("create");
      router.push("/login");
      return;
    }

    conversationCreateInFlight.current = true;
    setCreatingConversation(true);
    setConversationError("");
    setConversationRetryAction(null);

    const slowTimer = window.setTimeout(() => {
      setConversationError("建立新對話需要較長時間，請稍候。");
    }, 10000);

    try {
      const createdAt = new Date().toISOString();
      const conversation = {
        ...(await createConversation(userId)),
        created_at: createdAt,
        updated_at: createdAt,
      };

      setConversationError("");
      draftConversationsRef.current.set(
        conversation.conversation_id,
        conversation,
      );
      setConversations((current) => [
        conversation,
        ...current.filter(
          (item) => item.conversation_id !== conversation.conversation_id,
        ),
      ]);
      openConversation(conversation.conversation_id, true);
    } catch (error) {
      console.error("Create conversation failed:", error);
      setConversationError("無法建立新對話，請稍後重試。");
      setConversationRetryAction("create");
    } finally {
      window.clearTimeout(slowTimer);
      conversationCreateInFlight.current = false;
      setCreatingConversation(false);
    }
  }

  function openConversation(
    conversationId: string,
    draft = false,
  ) {
    const normalized =
      (
        conversationId ||
        "default"
      ).trim() ||
      "default";

    setActiveConversationId(
      normalized,
    );

    setMobileSidebarOpen(
      false,
    );

    const query = new URLSearchParams();

    query.set("conversation_id", normalized);

    if (draft) {
      query.set("draft", "1");
    }

    router.push("/chat?" + query.toString());
  }

  function openConversationMenu(
    event:
      ReactMouseEvent<
        HTMLButtonElement
      >,
    conversation:
      ConversationSummary,
  ) {
    event.preventDefault();
    event.stopPropagation();

    const menuWidth =
      190;

    const menuHeight =
      108;

    const triggerRect =
      event.currentTarget
        .getBoundingClientRect();

    const x =
      Math.max(
        8,
        Math.min(
          triggerRect.right
            - menuWidth,
          window.innerWidth
            - menuWidth
            - 8,
        ),
      );

    const y =
      Math.max(
        8,
        Math.min(
          triggerRect.bottom + 6,
          window.innerHeight
            - menuHeight
            - 8,
        ),
      );

    setConversationContextMenu({
      conversationId:
        conversation
          .conversation_id,

      conversationTitle:
        getConversationTitle(
          conversation,
        ),

      x,
      y,
    });
  }

  async function handleRenameConversation() {
    const selected = conversationContextMenu;
    if (!selected) return;

    setConversationContextMenu(null);
    const value = window.prompt(
      "重新命名對話（最多 80 字）",
      selected.conversationTitle,
    );
    if (value === null) return;

    const title = value.trim().replace(/\s+/g, " ");
    if (!title || Array.from(title).length > 80) {
      setConversationError("標題請輸入 1～80 個字。");
      return;
    }

    const userId = getCurrentUserId();
    if (!userId) {
      router.push("/login");
      return;
    }

    try {
      const updated = await renameConversation(
        userId,
        selected.conversationId,
        title,
      );
      setConversations(current => current.map(item =>
        item.conversation_id === selected.conversationId
          ? updated
          : item,
      ));
      setConversationError("");
    } catch {
      setConversationError("重新命名失敗，原標題已保留，請再試一次。");
    }
  }

  async function handleDeleteConversation() {
    const selected =
      conversationContextMenu;

    if (!selected) {
      return;
    }

    const userId =
      getCurrentUserId();

    if (!userId) {
      setConversationContextMenu(
        null,
      );

      router.push(
        "/login",
      );

      return;
    }

    const confirmed =
      window.confirm(
        `確定要刪除「${
          selected.conversationTitle
        }」嗎？\n\n刪除後無法復原。`,
      );

    if (!confirmed) {
      setConversationContextMenu(
        null,
      );

      return;
    }

    setDeletingConversationId(
      selected.conversationId,
    );

    try {
      await deleteConversation(
        userId,
        selected.conversationId,
      );

      const remainingConversations =
        conversations.filter(
          conversation =>
            conversation
              .conversation_id
            !== selected
              .conversationId,
        );

      setConversations(
        remainingConversations,
      );
      setConversationTotal((current) => Math.max(0, current - 1));
      conversationServerOffset.current = Math.max(
        0, conversationServerOffset.current - 1,
      );

      setConversationContextMenu(
        null,
      );

      if (
        activeConversationId
        === selected.conversationId
      ) {
        if (
          remainingConversations
            .length > 0
        ) {
          openConversation(
            remainingConversations[0]
              .conversation_id,
          );

        } else {
          try {
            const newConversation =
              await createConversation(
                userId,
              );

            setConversations([]);

            openConversation(newConversation.conversation_id, true);

          } catch (
            createError
          ) {
            console.error(
              "Create conversation after delete failed:",
              createError,
            );

            setActiveConversationId(
              "default",
            );

            router.push(
              "/chat",
            );
          }
        }
      }

      window.dispatchEvent(
        new Event(
          "blink-conversations-updated",
        ),
      );

    } catch (
      error
    ) {
      console.error(
        "Delete conversation failed:",
        error,
      );

      window.alert(
        "刪除對話失敗，請稍後再試。",
      );

    } finally {
      setDeletingConversationId(
        "",
      );
    }
  }

  useEffect(() => {
    function closeConversationMenu() {
      setConversationContextMenu(
        null,
      );
    }

    function handleMenuKeyDown(
      event: KeyboardEvent,
    ) {
      if (
        event.key ===
        "Escape"
      ) {
        closeConversationMenu();
      }
    }

    window.addEventListener(
      "click",
      closeConversationMenu,
    );

    window.addEventListener(
      "resize",
      closeConversationMenu,
    );

    window.addEventListener(
      "scroll",
      closeConversationMenu,
      true,
    );

    window.addEventListener(
      "keydown",
      handleMenuKeyDown,
    );

    return () => {
      window.removeEventListener(
        "click",
        closeConversationMenu,
      );

      window.removeEventListener(
        "resize",
        closeConversationMenu,
      );

      window.removeEventListener(
        "scroll",
        closeConversationMenu,
        true,
      );

      window.removeEventListener(
        "keydown",
        handleMenuKeyDown,
      );
    };
  }, []);

  /* =======================================================
  URL → Active Conversation
  ======================================================= */

  useEffect(() => {
    if (
      typeof window ===
      "undefined"
    ) {
      return;
    }

    const params =
      new URLSearchParams(
        window.location.search,
      );

    setActiveConversationId(
      params.get(
        "conversation_id",
      ) ||
        "default",
    );
  }, [
    pathname,
  ]);

  /* =======================================================
  Conversation Updated Event

   ChatPage 建立新對話
   或更新標題後通知 AppShell。

  ======================================================= */

  useEffect(() => {
    function handleActiveConversation(event: Event) {
      setActiveConversationId(
        (event as CustomEvent<string>).detail || "default",
      );
    }

    window.addEventListener(
      "blink-active-conversation",
      handleActiveConversation,
    );
    return () => window.removeEventListener(
      "blink-active-conversation",
      handleActiveConversation,
    );
  }, []);

  useEffect(() => {
    function handleUpdated(event: Event) {
      const detail = (event as CustomEvent<{
        conversationId?: string;
        title?: string;
      }>).detail;
      const conversationId = detail?.conversationId?.trim();

      if (conversationId) {
        setActiveConversationId(conversationId);
        setConversations((current) => {
          const existing = current.find(
            (item) => item.conversation_id === conversationId,
          );
          const optimistic = {
            ...(existing ?? {}),
            conversation_id: conversationId,
            title: detail.title?.trim() || existing?.title || "新對話",
            updated_at: new Date().toISOString(),
          } as ConversationSummary;
          draftConversationsRef.current.set(conversationId, optimistic);
          return sortConversationsNewestFirst([
            optimistic,
            ...current.filter(
              (item) => item.conversation_id !== conversationId,
            ),
          ]);
        });
      }

    }

    window.addEventListener(
      "blink-conversations-updated",
      handleUpdated,
    );

    return () => {
      window.removeEventListener(
        "blink-conversations-updated",
        handleUpdated,
      );
    };

    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    conversationOpen,
  ]);

  /* =======================================================
  Chat Page
  ======================================================= */

  const isChatPage =
    pathname ===
    "/chat";

  /* =======================================================
  Login Pages
  ======================================================= */

    const isAdminPage = pathname.startsWith("/admin");

const isLoginPage =
    pathname.startsWith(
      "/login",
    );

  if (
          isLoginPage ||
      isAdminPage
  ) {
    return (
      <>
        {children}
      </>
    );
  }

  /* =======================================================
  Sidebar Account Name
  ======================================================= */

  const accountName =
    displayName ||
    "我的 BLINK";

  const providerLabel =
    getProviderLabel(
      authProvider,
    );

  /* =======================================================
  Render
  ======================================================= */

  return (
    <div className="app-shell">

      {/* ===================================================
          Mobile Top Bar
      =================================================== */}

      <header className="mobile-app-bar">

        <button
          ref={mobileMenuButtonRef}
          type="button"
          className="mobile-menu-button"
          aria-label="開啟選單"
          aria-controls="primary-sidebar"
          aria-expanded={
            mobileSidebarOpen
          }
          onClick={() => {
            setConversationContextMenu(
              null,
            );

            setMobileSidebarOpen(
              true,
            );
          }}
        >
          <span aria-hidden="true" />
          <span aria-hidden="true" />
          <span aria-hidden="true" />
        </button>

        <Link
          href="/"
          className="mobile-app-brand"
          aria-label="回到 BLINK AI 首頁"
          onClick={() =>
            setMobileSidebarOpen(
              false,
            )
          }
        >
          <strong>
            BLINK AI
          </strong>

          <small>
            MY BEAUTY COMPANION
          </small>
        </Link>

        <span
          className="mobile-app-bar-balance"
          aria-hidden="true"
        />

      </header>

      <button
        type="button"
        className={`mobile-sidebar-backdrop ${
          mobileSidebarOpen
            ? "is-visible"
            : ""
        }`}
        aria-label="關閉選單"
        tabIndex={
          mobileSidebarOpen
            ? 0
            : -1
        }
        onClick={() =>
          setMobileSidebarOpen(
            false,
          )
        }
      />

      {/* ===================================================
          Sidebar
      =================================================== */}

      <aside
        id="primary-sidebar"
        className={`sidebar ${
          mobileSidebarOpen
            ? "is-mobile-open"
            : ""
        }`}
      >

        <div className="mobile-sidebar-heading">
          <button
            ref={mobileSidebarCloseRef}
            type="button"
            className="mobile-sidebar-close"
            aria-label="關閉選單"
            onClick={() =>
              setMobileSidebarOpen(
                false,
              )
            }
          >
            <span aria-hidden="true" />
          </button>
        </div>

        <div
          className="sidebar-window-dots"
          aria-hidden="true"
        >
          <i />
        </div>

        <Link
          href="/"
          className="brand"
          aria-label="回到 BLINK AI 首頁"
          onClick={() =>
            setMobileSidebarOpen(
              false,
            )
          }
        >

          <span className="brand-main">
            BLINK AI
          </span>

          <span className="brand-sub">
            MY BEAUTY COMPANION
          </span>

        </Link>

        <nav
          className="nav-list"
          aria-label="主要導覽"
        >

          {navItems.map(
            (
              item,
            ) => {

              if (
                item.external
              ) {
                return (
                  <a
                    key={
                      item.href
                    }
                    href={
                      item.href
                    }
                    target="_blank"
                    rel="noopener noreferrer"
                    className="nav-item"
                    onClick={() =>
                      setMobileSidebarOpen(
                        false,
                      )
                    }
                  >

                    <span className="nav-icon">
                      <SidebarImageIcon
                        name={item.icon}
                      />
                    </span>

                    <span>
                      {item.label}
                    </span>

                    <span
                      className="nav-arrow"
                      aria-hidden="true"
                    >
                      ›
                    </span>

                  </a>
                );
              }

              const active =
                pathname ===
                  item.href ||
                (
                  item.href ===
                    "/profile" &&
                  pathname ===
                    "/personality-quiz"
                ) ||
                (
                  item.href ===
                    "/skin-report" &&
                  pathname ===
                    "/skin-record"
                );

              return (
                <Link
                  key={
                    item.href
                  }
                  href={
                    item.href
                  }
                  className={`nav-item ${
                    active
                      ? "is-active"
                      : ""
                  }`}
                  onClick={() =>
                    setMobileSidebarOpen(
                      false,
                    )
                  }
                >

                  <span className="nav-icon">
                    <SidebarImageIcon
                        name={item.icon}
                      />
                  </span>

                  <span>
                    {item.label}
                  </span>

                  <span
                    className="nav-arrow"
                    aria-hidden="true"
                  >
                    ›
                  </span>

                </Link>
              );
            },
          )}

        </nav>

        {/* =================================================
        對話紀錄
        ================================================= */}
        
        <div className="sidebar-chat-section">
        
          <button
            type="button"
            className={`nav-item sidebar-chat-toggle ${
              isChatPage
                ? "is-active"
                : ""
            }`}
            aria-expanded={
              conversationOpen
            }
            aria-controls="conversation-sidebar"
            onClick={() =>
              void toggleConversationPanel()
            }
          >
        
            <span className="nav-icon sidebar-chat-toggle-icon">
              <SidebarImageIcon
                name="chat"
              />
            </span>
        
            <span className="sidebar-chat-toggle-label">
              對話紀錄
            </span>
        
            <span
              className={`nav-arrow sidebar-chat-toggle-arrow ${
                conversationOpen
                  ? "is-open"
                  : ""
              }`}
              aria-hidden="true"
            >
              ›
            </span>
        
          </button>
        
          {conversationOpen && (
        
            <div
              id="conversation-sidebar"
              className="conversation-inline-panel"
            >

              <button
                type="button"
                className="conversation-new-button"
                onClick={() => void startNewConversation()}
                disabled={
                creatingConversation || conversationTotal >= conversationMax
              }
              aria-busy={creatingConversation}
              title={
                conversationTotal >= conversationMax
                  ? "已達對話上限，請先刪除舊對話"
                  : undefined
              }
              >
                ＋ 新對話
              </button>

              {conversationError && (
                <div className="conversation-action-error" role="alert">
                  <span>{conversationError}</span>
                  {conversationRetryAction && (
                    <button
                      type="button"
                      onClick={() => {
                        if (conversationRetryAction === "create") {
                          void startNewConversation();
                        } else {
                          void loadConversations();
                        }
                      }}
                    >
                      重試
                    </button>
                  )}
                </div>
              )}
        
              <div
                className="conversation-list"
                aria-live="polite"
              >
        
                {conversationLoading &&
                conversations.length ===
                  0 ? (
        
                  <p className="conversation-empty">
                    載入中…
                  </p>
        
                ) : conversations.length ===
                  0 ? (
        
                  <p className="conversation-empty">
                    尚無對話紀錄
                  </p>
        
                ) : (
        
                  conversations.map(
                    (
                      conversation,
                    ) => {
        
                      const active =
                        isChatPage &&
                        conversation
                          .conversation_id ===
                        activeConversationId;
        
                      const conversationTitle =
                        getConversationTitle(
                          conversation,
                        );
        
                      const isNewChat =
                        conversationTitle ===
                        "新對話";
        
                      return (
        
                        <div
                          key={
                            conversation
                              .conversation_id
                          }
                          className={`conversation-item ${
                            active
                              ? "is-active"
                              : ""
                          } ${
                            isNewChat
                              ? "is-new-chat"
                              : ""
                          }`}
                          title={
                            conversationTitle
                          }
                        >

                          <button
                            type="button"
                            className="conversation-item-open"
                            aria-current={active ? "page" : undefined}
                            onClick={() =>
                              openConversation(
                                conversation.conversation_id,
                                draftConversationsRef.current.has(
                                  conversation.conversation_id,
                                ),
                              )
                            }
                          >
                            <span className="conversation-item-title">
                              {conversationTitle}
                            </span>
                            <span className="conversation-item-meta">
                              <time dateTime={conversation.updated_at || conversation.created_at}>
                                {formatConversationTime(
                                  conversation.updated_at || conversation.created_at,
                                )}
                              </time>
                            </span>
                          </button>

                          <button
                            type="button"
                            className="conversation-item-more"
                            aria-label={`開啟「${conversationTitle}」的操作選單`}
                            aria-haspopup="menu"
                            aria-expanded={
                              conversationContextMenu
                                ?.conversationId ===
                              conversation
                                .conversation_id
                            }
                            onClick={event =>
                              openConversationMenu(
                                event,
                                conversation,
                              )
                            }
                          >
                            <span
                              className="conversation-more-dots"
                              aria-hidden="true"
                            >
                              <i />
                              <i />
                              <i />
                            </span>
                          </button>

                        </div>
        
                      );
                    },
                  )
        
                )}
        
                          {conversationHasMore && conversations.length > 0 && (
              <button
                type="button"
                className="conversation-load-more"
                disabled={conversationLoadingMore}
                onClick={() => void loadConversations({ append: true })}
              >
                {conversationLoadingMore ? "載入中…" : "載入較早的對話"}
              </button>
            )}
            {conversationTotal > 0 && (
              <p className="conversation-result-count">
                已顯示 {Math.min(conversations.length, conversationTotal)}／{conversationTotal} 筆
              </p>
            )}

          </div>
        
            </div>
        
          )}

          {conversationContextMenu && (

            <div
              className="conversation-context-menu"
              role="menu"
              aria-label="對話操作"
              style={{
                left:
                  conversationContextMenu.x,

                top:
                  conversationContextMenu.y,
              }}
              onClick={event =>
                event.stopPropagation()
              }
              onContextMenu={event =>
                event.preventDefault()
              }
            >

              <button
                type="button"
                role="menuitem"
                className="conversation-context-rename"
                onClick={() => void handleRenameConversation()}
              >
                <svg
                  className="conversation-context-rename-icon"
                  viewBox="0 0 24 24"
                  aria-hidden="true"
                >
                  <path d="M4 20h4L19 9a2.8 2.8 0 0 0-4-4L4 16v4Z" />
                  <path d="m13.5 6.5 4 4" />
                </svg>
                <span>重新命名</span>
              </button>

              <button
                type="button"
                role="menuitem"
                className="conversation-context-delete"
                disabled={
                  deletingConversationId
                  === conversationContextMenu
                    .conversationId
                }
                onClick={() =>
                  void handleDeleteConversation()
                }
              >

                <svg
                  className="conversation-context-delete-icon"
                  viewBox="0 0 24 24"
                  aria-hidden="true"
                >
                  <path d="M4 7h16M9 7V4h6v3m-9 0 1 13h10l1-13M10 11v5m4-5v5" />
                </svg>

                <span>
                  {deletingConversationId
                    === conversationContextMenu
                      .conversationId
                    ? "刪除中…"
                    : "刪除對話"}
                </span>

              </button>

            </div>

          )}
        
        </div>

        {/* =================================================
            Account / Login Footer
        ================================================= */}

        <div className="sidebar-footer">

          {!authReady ? (

            <div className="sidebar-account-card" aria-busy="true" aria-live="polite">
              <span className="sidebar-account-copy">
                <strong>正在確認登入狀態</strong>
                <small>請稍候…</small>
              </span>
            </div>

          ) : isLoggedIn ? (

            <div
              className="
                sidebar-account-card
                sidebar-account-card--logged-in
              "
            >

              <span className="sidebar-account-copy">

                <strong>
                  {accountName}
                </strong>

                <small>
                  已登入
                </small>

              </span>

              <button
                type="button"
                className="sidebar-account-menu-button"
                aria-label="開啟會員選單"
                aria-haspopup="menu"
                aria-expanded={
                  accountMenuOpen
                }
                onClick={() =>
                  setAccountMenuOpen(
                    (current) =>
                      !current,
                  )
                }
              >
                <span aria-hidden="true" />
                <span aria-hidden="true" />
                <span aria-hidden="true" />
              </button>

              {accountMenuOpen && (

                <div
                  className="sidebar-account-menu"
                  role="menu"
                >

                  <Link
                    href="/profile"
                    role="menuitem"
                    className="sidebar-account-menu-item"
                    onClick={() => {
                      setAccountMenuOpen(
                        false,
                      );

                      setMobileSidebarOpen(
                        false,
                      );
                    }}
                  >
                    <Icon name="user" />
                    <span>基本資料</span>
                  </Link>

                  <button
                    type="button"
                    role="menuitem"
                    className="sidebar-account-menu-item sidebar-account-menu-item--logout"
                    onClick={
                      handleLogout
                    }
                  >
                    <span
                      className="sidebar-account-logout-icon"
                      aria-hidden="true"
                    >
                      →
                    </span>
                    <span>登出</span>
                  </button>

                </div>

              )}

            </div>

          ) : (

            <Link
              href="/login"
              className="sidebar-account-card"
              onClick={() =>
                setMobileSidebarOpen(
                  false,
                )
              }
            >

              <span className="sidebar-account-copy">

                <strong>
                  登入 BLINK AI
                </strong>

                <small>
                  保存你的膚況與偏好
                </small>

              </span>

              <span
                className="sidebar-account-arrow"
                aria-hidden="true"
              >
                →
              </span>

            </Link>

          )}

          <span className="sidebar-copyright">
            BLINK AI © 2026
          </span>

        </div>

      </aside>

      {/* ===================================================
          Main
      =================================================== */}

      <main
        className={`main-content ${
          isChatPage
            ? "chat-main-content"
            : ""
        }`}
      >

        {!isChatPage && (

          <div className="main-topbar">

            <div className="mini-logo">
              BLINK ♡
            </div>

            <div
              className="topbar-charms"
              aria-hidden="true"
            >

              <span>
                ✦
              </span>

              <span>
                ♡
              </span>

              <span>
                ✦
              </span>

            </div>

            <div className="topbar-pill">
              BLINK AI
            </div>

          </div>

        )}

        {children}

      </main>

    </div>
  );
}
