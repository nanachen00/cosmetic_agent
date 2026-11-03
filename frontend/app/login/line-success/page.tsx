"use client";

import {
  Suspense,
  useEffect,
} from "react";

import {
  useRouter,
} from "next/navigation";

import { browserMemberAuthUrl } from "@/lib/api-origin";

const POST_LOGIN_REDIRECT_KEY = "blink_post_login_redirect";

function safeNextPath(value: string | null): string {
  if (!value || !value.startsWith("/") || value.startsWith("//")) {
    return "/";
  }
  return value;
}



/* =========================================================
   LINE Login Success Page
========================================================= */

function LineLoginSuccessContent() {
  const router =
    useRouter();


  /* =======================================================
     Save Login Identity
  ======================================================= */

  useEffect(() => {
    let cancelled = false;

    async function completeLogin() {
      try {
        let response: Response | null = null;

        // OAuth callback 已先建立 session，再導回這個頁面。若資料庫或
        // 反向代理剛好稍慢，給 session 一個短暫的同步時間，避免第一次
        // 401 就立刻跳回登入頁形成循環。
        for (let attempt = 0; attempt < 3; attempt += 1) {
          response = await fetch(
            browserMemberAuthUrl("/session"),
            {
              credentials: "include",
              cache: "no-store",
              headers: { Accept: "application/json" },
            },
          );

          if (response.ok) break;
          if (attempt < 2) {
            await new Promise((resolve) =>
              window.setTimeout(resolve, 400 * (attempt + 1)),
            );
          }
        }

        if (!response?.ok) {
          throw new Error("Session validation failed");
        }

        const payload = (await response.json()) as {
          account?: {
            user_id?: string;
            username?: string;
            display_name?: string;
            email?: string;
          };
        };
        const account = payload.account ?? (payload as NonNullable<typeof payload.account>);
        const userId = account?.user_id?.trim() ?? "";

        if (!account || !userId) {
          throw new Error("Session user is missing");
        }
        if (cancelled) return;

        window.dispatchEvent(
          new Event("blink-auth-updated"),
        );
        const nextPath = safeNextPath(
          window.sessionStorage.getItem(POST_LOGIN_REDIRECT_KEY),
        );
        window.sessionStorage.removeItem(POST_LOGIN_REDIRECT_KEY);
        router.replace(nextPath);
      } catch {
        if (!cancelled) {
          const nextPath = safeNextPath(
            window.sessionStorage.getItem(POST_LOGIN_REDIRECT_KEY),
          );
          window.sessionStorage.removeItem(POST_LOGIN_REDIRECT_KEY);
          router.replace(
            `/login?next=${encodeURIComponent(nextPath)}&oauth_error=session_not_created`,
          );
        }
      }
    }

    void completeLogin();
    return () => {
      cancelled = true;
    };
  }, [router]);


  /* =======================================================
     Render
  ======================================================= */

  return (
    <main className="login-page">

      <section
        className="
          login-window
          login-window-single
        "
      >

        {/* =========================
            Decoration
        ========================== */}

        <div
          className="login-deco"
          aria-hidden="true"
        >
          ✦ ♡ ✦
        </div>


        {/* =========================
            Login Success
        ========================== */}

        <div
          style={{
            minHeight:
              "280px",

            display:
              "flex",

            flexDirection:
              "column",

            alignItems:
              "center",

            justifyContent:
              "center",

            gap:
              "12px",

            textAlign:
              "center",
          }}
        >

          <span className="eyebrow-badge">
            LINE LOGIN
          </span>


          <h1
            style={{
              margin:
                0,

              color:
                "#60484d",
            }}
          >
            LINE 登入成功！
          </h1>


          <p
            style={{
              margin:
                0,

              color:
                "#99878d",
            }}
          >
            正在帶你回到 BLINK AI…
          </p>

        </div>

      </section>

    </main>
  );
}

export default function LineLoginSuccessPage() {
  return (
    <Suspense
      fallback={
        <main className="login-page">
          <section
            className="
              login-window
              login-window-single
            "
          >
            <div
              style={{
                minHeight: "280px",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
              }}
            >
              LINE 登入處理中…
            </div>
          </section>
        </main>
      }
    >
      <LineLoginSuccessContent />
    </Suspense>
  );
}
