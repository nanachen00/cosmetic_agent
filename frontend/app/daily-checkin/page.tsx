"use client";

import {
  useEffect,
  useRef,
  useState,
} from "react";

import {
  useRouter,
} from "next/navigation";

import Image from "next/image";

import {
  PageHeader,
} from "@/components/PageHeader";

import {
  getDailyContents,
  getCheckinStatus,
  submitDailyCheckin,
  type CheckinStatus,
  type DailyContentsResponse,
} from "@/lib/checkin-api";

import {
  readSession,
  writeSession,
} from "@/lib/storage";

import {
  getCurrentAuthIdentity,
} from "@/lib/auth-client";


const TOTAL_DAYS = 14;

const CHECKIN_CACHE_PREFIX =
  "blink-checkin-status:";


function getCheckinCacheKey(
  userId: string
) {
  return `${CHECKIN_CACHE_PREFIX}${userId}`;
}


function isStatusForUser(
  value: CheckinStatus | null,
  userId: string
): value is CheckinStatus {
  return value?.user_id === userId;
}

const FALLBACK_DAILY_CONTENTS:
DailyContentsResponse["contents"] = {
  beauty_tip: {
    id: "fallback-beauty-tip",
    content_type: "beauty_tip",
    title: "保養不是步驟越多越好，而是越適合自己越好。",
    summary: "從清潔、保濕與防曬開始，建立能長期維持的基礎習慣。",
    source_name: null,
    source_url: null,
    source_published_at: null,
    published_at: null,
  },
  dermatologist_faq: {
    id: "fallback-dermatologist-faq",
    content_type: "dermatologist_faq",
    title: "最近有肌膚問題嗎？先記錄症狀與持續時間。",
    summary: "點擊後可進入膚況諮詢，整理部位、程度與目前使用產品。",
    source_name: null,
    source_url: null,
    source_published_at: null,
    published_at: null,
  },
};


type ToastState = {
  visible: boolean;

  message: string;
};


/* =========================================================
   Beauty Icon
   ========================================================= */

function BeautyCompactIcon() {
  return (
    <Image
      src="/daily-icons/woman.png"
      alt=""
      width={512}
      height={512}
      className="reward-icon-image beauty-icon-image"
    />
  );
}


/* =========================================================
   Doctor Icon
   ========================================================= */

function DoctorHeartIcon() {
  return (
    <Image
      src="/daily-icons/medical-team.png"
      alt=""
      width={512}
      height={512}
      className="reward-icon-image doctor-icon-image"
    />
  );
}


/* =========================================================
   Page
   ========================================================= */

export default function DailyCheckinPage() {

  const router =
    useRouter();


  const [
    userId,
    setUserId,
  ] =
    useState("");


  const [
    status,
    setStatus,
  ] =
    useState<CheckinStatus | null>(
      null
    );


  const [
    loaded,
    setLoaded,
  ] =
    useState(false);


  const [
    submitting,
    setSubmitting,
  ] =
    useState(false);


  const [
    syncUnavailable,
    setSyncUnavailable,
  ] =
    useState(false);


  const [
    dailyContents,
    setDailyContents,
  ] = useState<DailyContentsResponse["contents"]>(
    FALLBACK_DAILY_CONTENTS,
  );


  const [
    toast,
    setToast,
  ] =
    useState<ToastState>({
      visible:
        false,

      message:
        "",
    });


  const toastTimerRef =
    useRef<
      ReturnType<typeof setTimeout>
      | null
    >(null);


  /* =======================================================
     Toast
     ======================================================= */

  function showToast(
    message: string
  ) {

    if (
      toastTimerRef.current
    ) {
      clearTimeout(
        toastTimerRef.current
      );
    }


    setToast({
      visible:
        true,

      message,
    });


    toastTimerRef.current =
      setTimeout(
        () => {

          setToast(
            (current) => ({
              ...current,

              visible:
                false,
            })
          );

        },
        2500
      );

  }


  /* =======================================================
     第一次進頁面：向後端取得簽到狀態
     ======================================================= */

  useEffect(() => {

    let cancelled =
      false;


    async function loadCheckin() {

      let identity;
      try {
        identity = await getCurrentAuthIdentity();
      } catch (error) {
        console.error("Failed to resolve current member session:", error);
        if (!cancelled) {
          setLoaded(true);
          setSyncUnavailable(true);
          showToast("目前無法確認登入狀態，請檢查連線後重試。");
        }
        return;
      }

      if (!identity) {
        if (!cancelled) {
          router.replace(
            "/login?next=%2Fdaily-checkin"
          );
        }
        return;
      }

      const uid = identity.userId;


      setUserId(
        uid
      );


      const cachedStatus =
        readSession<CheckinStatus | null>(
          getCheckinCacheKey(uid),
          null
        );


      if (
        !cancelled &&
        isStatusForUser(cachedStatus, uid)
      ) {
        setStatus(
          cachedStatus
        );
      }


      try {

        const result =
          await getCheckinStatus(
            uid
          );


        if (
          !isStatusForUser(result, uid)
        ) {
          throw new Error(
            "簽到資料與目前登入帳號不一致"
          );
        }


        if (
          !cancelled
        ) {
          setStatus(
            result
          );

          writeSession(
            getCheckinCacheKey(uid),
            result
          );

          setSyncUnavailable(
            false
          );
        }

      } catch (error) {

        console.error(
          "Failed to load daily check-in status:",
          error
        );


        if (
          !cancelled
        ) {

          setSyncUnavailable(
            true
          );


          showToast(
            isStatusForUser(cachedStatus, uid)
              ? "目前顯示上次同步的簽到紀錄，連線恢復後再更新。"
              : error instanceof Error
                ? error.message
                : "無法取得簽到資料"
          );

        }

      } finally {

        if (
          !cancelled
        ) {
          setLoaded(
            true
          );
        }

      }

    }


    loadCheckin();


    return () => {

      cancelled =
        true;

    };

  }, []);


  /* =======================================================
     清除 Toast Timer
     ======================================================= */

  useEffect(() => {

    return () => {

      if (
        toastTimerRef.current
      ) {

        clearTimeout(
          toastTimerRef.current
        );

      }

    };

  }, []);


  /* =======================================================
     每日內容：只使用 PostgreSQL 已審核發布資料
     ======================================================= */

  useEffect(() => {
    let cancelled = false;

    getDailyContents()
      .then((result) => {
        if (!cancelled && result.ok) {
          setDailyContents(result.contents);
        }
      })
      .catch(() => {
        // The page starts with reviewed fallback copy, so an empty editorial
        // schedule remains a valid, quiet state instead of a console error.
      });

    return () => {
      cancelled = true;
    };
  }, []);


  /* =======================================================
     Derived State
     ======================================================= */

  const currentCycle =
    status
      ?.current_cycle
      ?? 1;


  const completedCount =
    status
      ?.completed_count
      ?? 0;


  const currentCycleCheckins =
    status
      ?.current_cycle_checkins
      ?? [];


  const cycleCompleted =
    status
      ?.cycle_completed
      ?? false;


  const checkedInToday =
    status
      ?.today_checked_in
      ?? false;


  const points =
    status
      ?.points
      ?? 0;


  /* =======================================================
     Check-in
     ======================================================= */

  async function handleCheckin() {

    if (
      !loaded ||
      !status ||
      !userId ||
      submitting ||
      !status.can_check_in
    ) {
      return;
    }


    try {

      setSubmitting(
        true
      );


      const result =
        await submitDailyCheckin(
          userId
        );


      /*
       * 前端不自行 +10P，
       * 也不自行決定 Day 幾。
       *
       * 完全使用後端最新狀態。
       */
      if (
        !isStatusForUser(
          result.status,
          userId
        )
      ) {
        throw new Error(
          "簽到資料與目前登入帳號不一致"
        );
      }


      setStatus(
        result.status
      );


      writeSession(
        getCheckinCacheKey(userId),
        result.status
      );


      setSyncUnavailable(
        false
      );


      showToast(
        result.message
      );

    } catch (error) {

      console.error(
        "Daily check-in failed:",
        error
      );


      showToast(
        error instanceof Error
          ? error.message
          : "簽到失敗，請稍後再試"
      );

    } finally {

      setSubmitting(
        false
      );

    }

  }


  /* =======================================================
     Button Text
     ======================================================= */

  const checkinButtonText =
    !loaded
      ? "載入中..."
      : submitting
        ? "簽到中..."
        : syncUnavailable
          ? "同步暫時中斷"
          : cycleCompleted
          ? "本輪 14 天已完成"
          : checkedInToday
            ? "今日已簽到"
            : "今日簽到";


  /* =======================================================
     Render
     ======================================================= */

  if (!loaded) {
    return (
      <div className="content-wrap daily-page" aria-busy="true">
        <PageHeader title="每日簽到" />
        <section className="retro-frame checkin-card" role="status" aria-live="polite">
          <p>正在確認會員與簽到資料…</p>
        </section>
      </div>
    );
  }

  return (
    <div className="content-wrap daily-page">

      <PageHeader
        title="每日簽到"
      />


      {/* ===================================================
          14-Day Check-in
          =================================================== */}

      <section className="retro-frame checkin-card">

        {/* 我的三友點數固定最右邊 */}
        <div
          className="checkin-card-topline"
          style={{
            width: "100%",
            display: "flex",
            justifyContent: "flex-end",
            alignItems: "center",
          }}
        >

          <div className="points-ticket">

            <span>
              我的三友點數
            </span>


            <strong>
              {points}P
            </strong>

          </div>

        </div>


        {/* ===============================================
            14 天星星
            =============================================== */}

        <div
          className="stamp-grid"
          aria-label={`第 ${currentCycle} 輪 14 天簽到進度`}
        >

          {Array.from(
            {
              length:
                TOTAL_DAYS,
            },
            (
              _,
              index
            ) => {

              const day =
                index + 1;


              const entry =
                currentCycleCheckins.find(
                  (item) =>
                    item.day_number ===
                    day
                );


              const done =
                Boolean(
                  entry
                );


              return (
                <div
                  key={day}
                  className={`stamp-item ${
                    done
                      ? "done"
                      : ""
                  }`}
                >

                  <span>
                    第{day}天
                  </span>


                  <b
                    aria-label={
                      done
                        ? `第 ${day} 天已簽到`
                        : `第 ${day} 天尚未簽到`
                    }
                    title={
                      entry
                        ?.date
                        ? `簽到日期：${entry.date}`
                        : undefined
                    }
                  >
                    {done
                      ? "★"
                      : "☆"}
                  </b>

                </div>
              );

            }
          )}

        </div>


        {/* ===============================================
            Check-in Button + Toast
            =============================================== */}

        <div className="checkin-action-area">

          <button
            type="button"
            className={`glossy-primary checkin-button ${
              !loaded || submitting
                ? "is-loading"
                : ""
            }`}
            aria-busy={
              !loaded || submitting
            }
            onClick={
              handleCheckin
            }
            disabled={
              !loaded ||
              !status ||
              submitting ||
              syncUnavailable ||
              cycleCompleted ||
              checkedInToday
            }
          >
            {checkinButtonText}
          </button>


          <div
            className={`checkin-toast ${
              toast.visible
                ? "is-visible"
                : ""
            }`}
            role="status"
            aria-live="polite"
          >
            {toast.message}
          </div>

        </div>


        {/* ===============================================
            Day 14 Complete
            =============================================== */}

        {cycleCompleted ? (

          <p className="next-cycle-note">

            ♡ 本輪完成紀錄會保留至今天結束，
            明天進入頁面時會自動開始第{" "}
            {currentCycle + 1} 輪。

          </p>

        ) : null}

      </section>


      {/* ===================================================
          每日美妝小知識
          =================================================== */}

      <section className="magazine-card lavender-card">

        <div className="daily-knowledge-content">

          <div
            className="magazine-icon beauty-reward-icon"
            aria-hidden="true"
          >
            <BeautyCompactIcon />
          </div>


          <div>

            <span>
              每日美妝小知識
            </span>


            <h2>
              {dailyContents.beauty_tip.title}
            </h2>


            <p>
              {dailyContents.beauty_tip.summary}
            </p>

          </div>

        </div>

      </section>


      {/* ===================================================
          皮膚科醫師建議
          =================================================== */}

      <section className="magazine-card coffee-card">

        <div className="daily-knowledge-content doctor-card-content">

          <div
            className="doctor-reward-icon"
            aria-hidden="true"
          >
            <DoctorHeartIcon />
          </div>


          <div>

            <span>
              皮膚科醫師建議
            </span>


            <h2>
              {dailyContents.dermatologist_faq.title}
            </h2>


            <p>
              {dailyContents.dermatologist_faq.summary}
            </p>

          </div>

        </div>

      </section>

    </div>
  );
}
