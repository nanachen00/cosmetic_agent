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

/* =========================================================
Google Login Success Page
========================================================= */

function GoogleLoginSuccessContent() {
  const router =
    useRouter();

  /* =======================================================
  Save Login Identity
  ======================================================= */

  useEffect(() => {
    let cancelled = false;

    async function completeLogin() {
      try {
        const response = await fetch(
          browserMemberAuthUrl("/session"),
          {
            credentials: "include",
            cache: "no-store",
          },
        );

        if (!response.ok) {
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
            GOOGLE LOGIN
          </span>


          <h1
            style={{
              margin:
                0,

              color:
                "#60484d",
            }}
          >
            Google 登入成功！
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

export default function GoogleLoginSuccessPage() {
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
              Google 登入處理中…
            </div>
          </section>
        </main>
      }
    >
      <GoogleLoginSuccessContent />
    </Suspense>
  );
}
