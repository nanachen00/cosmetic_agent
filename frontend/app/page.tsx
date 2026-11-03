"use client";

import Link from "next/link";
import { Icon, type IconName } from "@/components/Icon";
import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import styles from "./HomePersonalityQuizFab.module.css";

/* =========================================================
   首頁功能卡
   ========================================================= */

const knowledgeMapEnabled =
  process.env.NEXT_PUBLIC_KNOWLEDGE_MAP_ENABLED !== "0";

const featureCards: Array<{
  title: string;
  text: string;
  href: string;
  icon: IconName;
  label: string;
  mobileOnly?: boolean;
}> = [
  {
    title: "找產品",
    text: "根據膚況與需求，推薦適合你的產品",
    href: "/chat?flow=product_search&new=1",
    icon: "search",
    label: "PICK",
  },
  {
    title: knowledgeMapEnabled ? "美妝知識地圖" : "膚況紀錄",
    text: knowledgeMapEnabled
      ? "探索膚況、成分與常見藥品名稱的關聯"
      : "記錄今天的膚況，追蹤肌膚變化",
    href: knowledgeMapEnabled ? "/knowledge-map" : "/chat?flow=skin_record&new=1",
    icon: knowledgeMapEnabled ? "chart" : "clipboard",
    label: knowledgeMapEnabled ? "MAP" : "LOG",
  },
  ...(knowledgeMapEnabled
    ? [
        {
          title: "膚況紀錄",
          text: "記錄並查看每一次的肌膚變化",
          href: "/skin-report",
          icon: "clipboard" as IconName,
          label: "LOG",
          mobileOnly: true,
        },
      ]
    : []),
  {
    title: "膚況諮詢",
    text: "描述你的肌膚問題，讓我們協助整理",
    href: "/chat?flow=skin_consult&new=1",
    icon: "chat",
    label: "ASK",
  },
  {
    title: "每日簽到",
    text: "記下今天的保養狀態，延續你的肌膚習慣",
    href: "/daily-checkin",
    icon: "calendar",
    label: "CHECK",
    mobileOnly: true,
  },
  {
    title: "人格測驗",
    text: "探索你的專屬美妝性格",
    href: "/chat?flow=customer_quiz&new=1",
    icon: "sparkle",
    label: "QUIZ",
    mobileOnly: true,
  },
];


function getClosestFeatureIndex(track: HTMLElement): number {
  const cards = Array.from(
    track.querySelectorAll<HTMLElement>("[data-feature-card]"),
  );
  if (!cards.length) return 0;

  const trackRect = track.getBoundingClientRect();
  const trackCenter = trackRect.left + track.clientWidth / 2;
  let closestIndex = 0;
  let closestDistance = Number.POSITIVE_INFINITY;

  cards.forEach((card, index) => {
    const rect = card.getBoundingClientRect();
    const distance = Math.abs(rect.left + rect.width / 2 - trackCenter);
    if (distance < closestDistance) {
      closestDistance = distance;
      closestIndex = index;
    }
  });

  return closestIndex;
}

type WeatherSnapshot = {
  location: string;
  temperature: number;
  humidity: number;
  uv: number;
  rain: number;
  icon: string;
  condition: string;
  advice: string;
};


/* =========================================================
   Home
   ========================================================= */

function updateMobileFeatureDepth(track: HTMLElement) {
  const cards = Array.from(
    track.querySelectorAll<HTMLElement>("[data-feature-card]"),
  );
  const prefersReducedMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)",
  ).matches;

  if (
    !window.matchMedia("(max-width: 768px)").matches ||
    prefersReducedMotion
  ) {
    const closestIndex = getClosestFeatureIndex(track);
    cards.forEach((card, index) => {
      card.classList.remove("is-carousel-active");
      card.style.removeProperty("--mobile-card-scale");
      card.style.removeProperty("--mobile-card-depth");
      card.style.removeProperty("--mobile-card-lift");
      card.style.removeProperty("--mobile-card-tilt");
      card.style.removeProperty("--mobile-card-opacity");
      if (prefersReducedMotion && index === closestIndex) {
        card.classList.add("is-carousel-active");
      }
    });
    return;
  }

  const trackRect = track.getBoundingClientRect();
  const trackCenter = trackRect.left + trackRect.width / 2;
  const depthRange = Math.max(trackRect.width * 0.54, 1);

  const cardRects = cards.map((card) => card.getBoundingClientRect());

  cards.forEach((card, index) => {
    const cardRect = cardRects[index];
    const signedDistance =
      (cardRect.left + cardRect.width / 2 - trackCenter) / depthRange;
    const clampedDistance = Math.max(-1, Math.min(1, signedDistance));
    const distance = Math.abs(clampedDistance);

    card.style.setProperty(
      "--mobile-card-scale",
      (1.08 - distance * 0.36).toFixed(3),
    );
    card.style.setProperty(
      "--mobile-card-depth",
      `${(-108 * distance).toFixed(1)}px`,
    );
    card.style.setProperty(
      "--mobile-card-lift",
      `${(22 * distance).toFixed(1)}px`,
    );
    card.style.setProperty(
      "--mobile-card-tilt",
      `${(clampedDistance * -14).toFixed(1)}deg`,
    );
    card.style.setProperty(
      "--mobile-card-opacity",
      (1 - distance * 0.5).toFixed(3),
    );
    card.classList.toggle("is-carousel-active", distance < 0.2);
  });
}


export default function HomePage() {
  const router = useRouter();

  const [message, setMessage] = useState("");
  const [assistantMessage] = useState("");
  const [loading] = useState(false);
  const [weather, setWeather] = useState<WeatherSnapshot | null>(null);
  const [weatherError, setWeatherError] = useState<string | null>(null);
  const [weatherRequestKey, setWeatherRequestKey] = useState(1);
  const homePageRef = useRef<HTMLDivElement>(null);
  const featureTrackRef = useRef<HTMLElement>(null);
  const featureFrameRef = useRef<number | null>(null);
  const activeFeatureIndexRef = useRef(0);
  const [activeFeatureIndex, setActiveFeatureIndex] = useState(0);

  useMemo(() => `web-${Date.now().toString(36)}`, []);

  useEffect(() => {
    let cancelled = false;
    const controller = new AbortController();

    async function loadWeather() {
      try {
        const query = new URLSearchParams({
          latitude: "25.0330",
          longitude: "121.5654",
          location: "臺北市",
        });
        const response = await fetch(`/api/weather?${query}`, {
          cache: "no-store",
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("WEATHER_UNAVAILABLE");

        const nextWeather = (await response.json()) as WeatherSnapshot;
        if (!cancelled) {
          setWeather(nextWeather);
          setWeatherError(null);
        }
      } catch (error) {
        if (!cancelled) {
          setWeather(null);
          setWeatherError("目前無法取得臺北市天氣");
        }
      }
    }

    void loadWeather();
    const intervalId = window.setInterval(loadWeather, 60 * 60 * 1000);

    return () => {
      cancelled = true;
      controller.abort();
      window.clearInterval(intervalId);
    };
  }, [weatherRequestKey]);

  useEffect(() => {
    const page = homePageRef.current;
    const viewport = window.visualViewport;

    if (!page || !viewport) return;

    const updateKeyboardInset = () => {
      const composerInputFocused =
        document.activeElement instanceof HTMLInputElement &&
        page.contains(document.activeElement);
      const inset = composerInputFocused
        ? Math.max(
            0,
            window.innerHeight - viewport.height - viewport.offsetTop,
          )
        : 0;
      page.style.setProperty(
        "--home-keyboard-inset",
        `${Math.round(inset)}px`,
      );
    };

    viewport.addEventListener("resize", updateKeyboardInset);
    viewport.addEventListener("scroll", updateKeyboardInset);
    page.addEventListener("focusin", updateKeyboardInset);
    page.addEventListener("focusout", updateKeyboardInset);
    updateKeyboardInset();

    return () => {
      viewport.removeEventListener("resize", updateKeyboardInset);
      viewport.removeEventListener("scroll", updateKeyboardInset);
      page.removeEventListener("focusin", updateKeyboardInset);
      page.removeEventListener("focusout", updateKeyboardInset);
      page.style.removeProperty("--home-keyboard-inset");
    };
  }, []);


  /* =======================================================
     Mobile feature carousel depth
     ======================================================= */

  useEffect(() => {
    const track = featureTrackRef.current;

    if (!track) {
      return;
    }

    const mobileQuery = window.matchMedia("(max-width: 768px)");
    const updateCardDepth = () => {
      if (featureFrameRef.current !== null) return;

      featureFrameRef.current = window.requestAnimationFrame(() => {
        featureFrameRef.current = null;
        updateMobileFeatureDepth(track);
        const nextIndex = getClosestFeatureIndex(track);
        if (activeFeatureIndexRef.current !== nextIndex) {
          activeFeatureIndexRef.current = nextIndex;
          setActiveFeatureIndex(nextIndex);
        }
      });
    };

    window.addEventListener("resize", updateCardDepth);
    mobileQuery.addEventListener("change", updateCardDepth);
    updateCardDepth();

    return () => {
      if (featureFrameRef.current !== null) {
        window.cancelAnimationFrame(featureFrameRef.current);
        featureFrameRef.current = null;
      }
      window.removeEventListener("resize", updateCardDepth);
      mobileQuery.removeEventListener("change", updateCardDepth);
    };
  }, []);


  /* =======================================================
     Submit
     ======================================================= */

  function submitMessage() {
    const trimmed = message.trim();

    if (!trimmed || loading) {
      return;
    }

    router.push(
      `/chat?flow=auto&new=1&message=${encodeURIComponent(trimmed)}`,
    );
  }

  /* =======================================================
     Render
     ======================================================= */

  return (
    <div
      ref={homePageRef}
      className={`content-wrap home-page ${styles.homePage}`}
    >
      {/* ===================================================
          Hero
          =================================================== */}

      <div className="home-hero-layout">
        <section className="home-hero home-greeting-card retro-frame">
          <div className="hero-sparkles" aria-hidden="true">
            <span>✦</span>
            <span>♡</span>
            <span>✦</span>
          </div>

          <div className="hero-copy">
            <h1 className="home-main-title home-brand-title">
              <span className="home-brand-primary">今天有什麼需要協助的呢</span>
            </h1>
          </div>
        </section>

        {weather ? (
          <aside
            className="hero-weather home-weather-card retro-frame"
            aria-label={
              "今日天氣：" +
              weather.location +
              " " +
              weather.temperature +
              " 度、" +
              weather.condition +
              "、濕度 " +
              weather.humidity +
              "%、紫外線 " +
              weather.uv +
              "、降雨 " +
              weather.rain +
              "%"
            }
          >
            <div className="home-weather-current">
              <span className="hero-weather-icon" aria-hidden="true">
                {weather.icon}
              </span>
              <strong>{weather.temperature}°</strong>
              <span className="home-weather-condition">{weather.condition}</span>
            </div>

            <div className="home-weather-metrics">
              <div className="home-weather-metric">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M12 2.5S5.5 10 5.5 15a6.5 6.5 0 0 0 13 0C18.5 10 12 2.5 12 2.5Z" />
                  <path d="M9 15.2a3.2 3.2 0 0 0 3.2 3.2" />
                </svg>
                <span><b>濕度</b><strong>{weather.humidity}%</strong></span>
              </div>
              <div className="home-weather-metric">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <circle cx="12" cy="12" r="4" />
                  <path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.4 1.4M17.6 17.6 19 19M19 5l-1.4 1.4M6.4 17.6 5 19" />
                </svg>
                <span><b>UV</b><strong>{weather.uv}</strong></span>
              </div>
              <div className="home-weather-metric">
                <svg viewBox="0 0 24 24" aria-hidden="true">
                  <path d="M6.5 16.5h11a4 4 0 0 0 .4-8A6 6 0 0 0 6.7 7.2a4.7 4.7 0 0 0-.2 9.3Z" />
                  <path d="m8 18-1 3M12 18l-1 3M16 18l-1 3" />
                </svg>
                <span><b>降雨</b><strong>{weather.rain}%</strong></span>
              </div>
            </div>

            <div className="home-weather-advice">
              <span className="home-weather-advice-copy">
                <b>今日保養建議</b>
                <p>{weather.advice}</p>
              </span>
            </div>
          </aside>
        ) : weatherError ? (
          <aside
            className="hero-weather home-weather-card home-weather-state retro-frame"
            aria-label={weatherError}
            role="status"
            aria-live="polite"
          >
            <button
              type="button"
              className="home-weather-location-button"
              onClick={() => setWeatherRequestKey((key) => key + 1)}
            >
              重新取得臺北市天氣
            </button>
            <p>{weatherError}</p>
          </aside>
        ) : (
          <aside
            className="hero-weather home-weather-card home-weather-state retro-frame"
            aria-label="正在取得臺北市天氣資訊"
            role="status"
            aria-live="polite"
          >
            <p>正在取得臺北市天氣與保養建議…</p>
          </aside>
        )}
      </div>

      {/* ===================================================
          功能卡
          =================================================== */}

      <section
        ref={(track) => {
          featureTrackRef.current = track;
          if (track) {
            updateMobileFeatureDepth(track);
          }
        }}
        className="feature-grid"
        aria-label="主要功能"
        onScroll={(event) => {
          const track = event.currentTarget;
          if (featureFrameRef.current !== null) return;

          featureFrameRef.current = window.requestAnimationFrame(() => {
            featureFrameRef.current = null;
            updateMobileFeatureDepth(track);
            const nextIndex = getClosestFeatureIndex(track);
            if (activeFeatureIndexRef.current !== nextIndex) {
              activeFeatureIndexRef.current = nextIndex;
              setActiveFeatureIndex(nextIndex);
            }
          });
        }}
      >
        {featureCards.map((card, index) => (
          <Link
            key={card.title}
            href={card.href}
            className={`feature-card feature-card-${index + 1}${card.mobileOnly ? " feature-card-mobile-only" : ""}${activeFeatureIndex === index ? " is-carousel-active" : ""}`}
            data-feature-card
            aria-label={`${card.title}：${card.text}`}
          >
            <span className="card-sticker">{card.label}</span>

            <span className="card-bow" aria-hidden="true">
              ୨୧
            </span>

            <div className="feature-icon">
              <Icon name={card.icon} className="feature-icon-image" />
            </div>

            <h2>{card.title}</h2>
            <p>{card.text}</p>
          </Link>
        ))}
      </section>
              <div className="home-feature-dots" role="group" aria-label="選擇首頁功能">
                {featureCards.map((card, index) => (
                  <button
                    key={`feature-dot-${card.title}`}
                    type="button"
                    className={`home-feature-dot${activeFeatureIndex === index ? " is-active" : ""}`}
                    aria-label={`移至${card.title}`}
                    aria-current={activeFeatureIndex === index ? "true" : undefined}
                    onClick={() => {
                      const target = featureTrackRef.current?.querySelectorAll<HTMLElement>(
                        "[data-feature-card]",
                      )[index];
                      target?.scrollIntoView({
                        behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches
                          ? "auto"
                          : "smooth",
                        block: "nearest",
                        inline: "center",
                      });
                      activeFeatureIndexRef.current = index;
                      setActiveFeatureIndex(index);
                    }}
                  />
                ))}
              </div>

      {/* ===================================================
          首頁輸入
          =================================================== */}

      <section className="composer-panel">
        {assistantMessage ? (
          <div className="clarify-bubble">{assistantMessage}</div>
        ) : null}

        <div className="composer-row">
          <input
            value={message}
            onChange={(event) => setMessage(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                submitMessage();
              }
            }}
            placeholder="請輸入您的需求"
            aria-label="請輸入您的需求"
          />

          <button
            type="button"
            onClick={submitMessage}
            disabled={loading || !message.trim()}
            aria-label="送出需求"
          >
            {loading ? (
              <span className="loading-dot">•••</span>
            ) : (
              <Icon name="send" />
            )}
          </button>
        </div>
      </section>

    </div>
  );
}
