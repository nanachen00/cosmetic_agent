"use client";

import Link from "next/link";
import { useEffect, useState } from "react";

import { Icon, type IconName } from "@/components/Icon";
import { PageHeader } from "@/components/PageHeader";
import { getCurrentAuthIdentity } from "@/lib/auth-client";
import {
  getCheckinStatus,
  type CheckinStatus,
} from "@/lib/checkin-api";
import {
  getPartnerSkinReportRecommendations,
  SkinTrackingApiError,
} from "@/lib/skin-tracking-api";
import { readSession, writeSession } from "@/lib/storage";
import type { SkinRecommendationsResponse } from "@/types/skin-tracking";

import styles from "./TomodsPage.module.css";

const TOMODS_HOME_URL = "https://www.tomods.com.tw/";
const TOMODS_MEMBER_URL = "https://www.tomods.com.tw/pages/member";
const CHECKIN_CACHE_PREFIX = "blink-checkin-status:";

type OfficialEntry = {
  label: string;
  title: string;
  description: string;
  href: string;
  icon?: IconName;
  image?: "fire";
};

const officialEntries: OfficialEntry[] = [
  {
    label: "BEST",
    title: "熱銷排行榜",
    description: "前往三友藥妝官網查看目前的人氣商品與熱銷排行。",
    href: "https://www.tomods.com.tw/collections/rank",
    image: "fire",
  },
  {
    label: "SALE",
    title: "買一送一專區",
    description: "查看三友藥妝官網最新買一送一品項與活動規則。",
    href: "https://www.tomods.com.tw/collections/onefree",
    icon: "gift",
  },
  {
    label: "NEW",
    title: "最新優惠 DM",
    description: "直接查看三友藥妝官網目前公開的最新檔期資訊。",
    href: "https://www.tomods.com.tw/pages/dms",
    icon: "sparkle",
  },
];

function getCheckinCacheKey(userId: string) {
  return `${CHECKIN_CACHE_PREFIX}${userId}`;
}

function isStatusForUser(
  value: CheckinStatus | null,
  userId: string,
): value is CheckinStatus {
  return value?.user_id === userId;
}

function formatPrice(price: number | null) {
  return typeof price === "number"
    ? `NT$${price.toLocaleString("zh-TW")}`
    : "價格請以三友官網為準";
}

export default function TomodsPage() {
  const [status, setStatus] = useState<CheckinStatus | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [signedIn, setSignedIn] = useState<boolean | null>(null);
  const [syncUnavailable, setSyncUnavailable] = useState(false);
  const [recommendations, setRecommendations] =
    useState<SkinRecommendationsResponse | null>(null);
  const [recommendationsLoading, setRecommendationsLoading] = useState(true);
  const [recommendationsError, setRecommendationsError] = useState("");

  useEffect(() => {
    let cancelled = false;

    async function loadMemberSummary() {
      try {
        const identity = await getCurrentAuthIdentity();

        if (!identity) {
          if (!cancelled) {
            setSignedIn(false);
            setLoaded(true);
            setRecommendationsLoading(false);
          }
          return;
        }

        const userId = identity.userId;
        const cacheKey = getCheckinCacheKey(userId);
        const cachedStatus = readSession<CheckinStatus | null>(cacheKey, null);

        if (!cancelled) {
          setSignedIn(true);
          if (isStatusForUser(cachedStatus, userId)) {
            setStatus(cachedStatus);
          }
        }

        try {
          const result = await getCheckinStatus(userId);

          if (!isStatusForUser(result, userId)) {
            throw new Error("簽到資料與目前登入帳號不一致");
          }

          if (!cancelled) {
            setStatus(result);
            setSyncUnavailable(false);
            writeSession(cacheKey, result);
          }
        } catch (error) {
          console.error("Failed to load Tomod's member summary:", error);
          if (!cancelled) setSyncUnavailable(true);
        }

        try {
          const result = await getPartnerSkinReportRecommendations(
            14,
            4,
            undefined,
            userId,
          );
          if (!cancelled) {
            setRecommendations(result);
            setRecommendationsError("");
          }
        } catch (error) {
          console.error("Failed to load Tomod's recommendations:", error);
          if (!cancelled) {
            setRecommendationsError(
              error instanceof SkinTrackingApiError && error.status === 401
                ? "請重新登入後查看個人化推薦。"
                : "目前無法取得個人化推薦，請稍後再試。",
            );
          }
        } finally {
          if (!cancelled) setRecommendationsLoading(false);
        }
      } catch (error) {
        console.error("Failed to verify the current session:", error);
        if (!cancelled) {
          setSyncUnavailable(true);
          setRecommendationsLoading(false);
          setRecommendationsError("目前無法確認登入狀態，請稍後再試。");
        }
      } finally {
        if (!cancelled) setLoaded(true);
      }
    }

    loadMemberSummary();

    return () => {
      cancelled = true;
    };
  }, []);

  const pointsText = !loaded
    ? "同步中…"
    : !signedIn
      ? "登入後查看"
      : typeof status?.points === "number"
        ? `${status.points}P`
        : "暫時無法取得";

  const partnerRecommendations =
    recommendations?.products.filter(
      (product) => product.is_partner && product.name.trim(),
    ) ?? [];

  return (
    <div className={`content-wrap tomods-page ${styles.page}`}>
      <PageHeader
        title="三友藥妝 Tomod's 專區"
      />

      <section className={styles.summarySection} aria-labelledby="summary-title">
        <div className={styles.sectionHeading}>
          <div>
            <span>MY BENEFITS</span>
            <h2 id="summary-title">我的三友藥妝專區資訊</h2>
          </div>
        </div>

        <div className={styles.summaryGrid}>
          <article className={`${styles.summaryCard} ${styles.pointsCard}`}>
            <div className={styles.cardIcon}>
              <span
                className={`${styles.pngIcon} ${styles.starIcon}`}
                aria-hidden="true"
              />
            </div>
            <div>
              <span>我的 BLINK 簽到點數</span>
              <strong className={styles.pointsValue}>{pointsText}</strong>
              <p>
                {syncUnavailable
                  ? status
                    ? "目前顯示本次登入期間的上次同步資料。"
                    : "目前無法連線取得簽到點數，請稍後再試。"
                  : "完成每日簽到即可繼續累積；三友會員權益仍以官網帳戶為準"}
              </p>
            </div>

            {!signedIn && loaded ? (
              <Link className={styles.textLink} href="/login?next=%2Ftomods">
                登入查看點數 <Icon name="arrow" />
              </Link>
            ) : (
              <Link className={styles.textLink} href="/daily-checkin">
                查看簽到紀錄 <Icon name="arrow" />
              </Link>
            )}
          </article>

          <article className={styles.summaryCard}>
            <div className={styles.cardIcon}>
              <span
                className={`${styles.pngIcon} ${styles.promoCodeIcon}`}
                aria-hidden="true"
              />
            </div>
            <div>
              <span>我的三友藥妝折價券</span>
              <strong>由三友官網管理</strong>
              <p>BLINK AI 尚未取得三友會員帳戶的折價券資料，避免顯示錯誤餘額。</p>
            </div>
            <a
              className={styles.textLink}
              href={TOMODS_MEMBER_URL}
              target="_blank"
              rel="noopener noreferrer"
            >
              前往會員中心 <Icon name="arrow" />
            </a>
          </article>

        </div>
      </section>

      <section
        className={styles.recommendationsSection}
        aria-labelledby="recommendations-title"
      >
        <div className={styles.sectionHeading}>
          <div>
            <h2 id="recommendations-title">適合你的三友藥妝商品</h2>
          </div>
          <p>依近期膚況紀錄與基本膚質篩選，推薦結果僅作日常保養選購參考。</p>
        </div>

        {recommendationsLoading ? (
          <div className={styles.recommendationStatus} aria-live="polite">
            正在根據近期膚況整理三友藥妝商品…
          </div>
        ) : !signedIn ? (
          <div className={styles.recommendationStatus}>
            <p>登入後即可根據你的膚況紀錄查看三友藥妝商品推薦。</p>
            <Link className={styles.textLink} href="/login?next=%2Ftomods">
              登入查看推薦 <Icon name="arrow" />
            </Link>
          </div>
        ) : recommendationsError ? (
          <div className={styles.recommendationStatus} role="alert">
            <p>{recommendationsError}</p>
          </div>
        ) : partnerRecommendations.length ? (
          <>
            <p className={styles.recommendationBasis}>{recommendations?.basis}</p>
            <div className={styles.recommendationGrid}>
              {partnerRecommendations.map((product) => (
                <article className={styles.recommendationCard} key={product.id}>
                  <span className={styles.partnerBadge}>三友藥妝商品</span>
                  <p className={styles.productBrand}>
                    {product.brand || "品牌資訊請以商品標示為準"}
                  </p>
                  <h3>{product.name}</h3>
                  {product.category ? (
                    <span className={styles.productCategory}>{product.category}</span>
                  ) : null}
                  <p className={styles.productReason}>{product.reason}</p>
                          {product.concerns_zh?.length ? <p className={styles.productReason}>對應膚況：{product.concerns_zh.join("、")}</p> : null}
                  {product.ingredients.length ? (
                    <p className={styles.productIngredients}>
                      資料標示成分：{product.ingredients.join("、")}
                    </p>
                  ) : null}
                  <strong className={styles.productPrice}>
                    {formatPrice(product.price)}
                  </strong>
                </article>
              ))}
            </div>
            <a
              className={styles.recommendationAction}
              href={TOMODS_HOME_URL}
              target="_blank"
              rel="noopener noreferrer"
            >
              前往三友藥妝官網確認商品資訊 <Icon name="arrow" />
            </a>
          </>
        ) : (
          <div className={styles.recommendationStatus}>
            <p>
              {recommendations?.message ||
                "目前還沒有足夠的膚況依據可篩選三友藥妝商品。"}
            </p>
            <Link className={styles.textLink} href="/chat?flow=skin_record&new=1">
              新增膚況紀錄 <Icon name="arrow" />
            </Link>
          </div>
        )}
      </section>

      <section className={styles.collectionsSection} aria-labelledby="collections-title">
        <div className={styles.sectionHeading}>
          <div>
            <h2 id="collections-title">三友藥妝官網精選入口</h2>
          </div>
          <p>以下連結直接開啟三友藥妝官網；商品、價格、庫存與優惠以官網即時內容為準。</p>
        </div>

        <div className={styles.collectionGrid}>
          {officialEntries.map((item) => (
            <a
              className={styles.collectionCard}
              href={item.href}
              key={item.title}
              target="_blank"
              rel="noopener noreferrer"
            >
              <span className={styles.collectionLabel}>{item.label}</span>
              <div className={styles.collectionIcon}>
                {item.image === "fire" ? (
                  <span
                    className={`${styles.pngIcon} ${styles.fireIcon}`}
                    aria-hidden="true"
                  />
                ) : item.icon === "gift" ? (
                  <span
                    className={`${styles.pngIcon} ${styles.giftIcon}`}
                    aria-hidden="true"
                  />
                ) : item.icon === "sparkle" ? (
                  <span
                    className={`${styles.pngIcon} ${styles.shiningIcon}`}
                    aria-hidden="true"
                  />
                ) : item.icon ? (
                  <Icon name={item.icon} />
                ) : null}
              </div>
              <h3>{item.title}</h3>
              <p>{item.description}</p>
              <span className={styles.collectionLink}>
                前往三友藥妝官網 <Icon name="arrow" />
              </span>
            </a>
          ))}
        </div>
      </section>
    </div>
  );
}
