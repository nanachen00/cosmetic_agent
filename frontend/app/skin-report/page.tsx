"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { Icon } from "@/components/Icon";
import {
  getCurrentSkinReport,
  SkinTrackingApiError
} from "@/lib/skin-tracking-api";
import {
  buildRegionMapTags,
  buildRegionSummaries
} from "@/lib/skin-report";
import { getCurrentAuthIdentity } from "@/lib/auth-client";
import type {
  FaceRegion,
  SkinReportResponse
} from "@/types/skin-tracking";

type ReportErrorInfo = {
  status: number;
  title: string;
  message: string;
};

function reportErrorMessage(error: unknown): ReportErrorInfo {
  if (error instanceof SkinTrackingApiError) {
    if (error.status === 401 || error.status === 403) {
      return {
        status: error.status,
        title: "登入後就能查看你的膚況報告",
        message: "登入同一個 BLINK AI 帳號後，就能安全讀取你的紀錄。"
      };
    }
    if (error.status === 408) {
      return {
        status: error.status,
        title: "這次整理花得比較久",
        message: "請重新整理一次；你的原始膚況紀錄不會遺失。"
      };
    }
    if (error.status === 503) {
      return {
        status: error.status,
        title: "膚況分析服務暫時忙碌",
        message: "請稍後再試，你已儲存的紀錄仍會保留。"
      };
    }
  }
  return {
    status: 0,
    title: "目前無法顯示膚況報告",
    message: "網路或服務可能短暫不穩定，請重新整理一次。"
  };
}



const WEEKDAY_SHORT = ["日", "一", "二", "三", "四", "五", "六"];

function toLocalIso(date: Date): string {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return year + "-" + month + "-" + day;
}

function startOfWeek(dateValue: string): string {
  const date = new Date(dateValue + "T00:00:00");
  date.setDate(date.getDate() - date.getDay());
  return toLocalIso(date);
}

function datesInWeek(start: string): string[] {
  const firstDay = new Date(start + "T00:00:00");
  return Array.from({ length: 7 }, (_, index) => {
    const date = new Date(firstDay);
    date.setDate(firstDay.getDate() + index);
    return toLocalIso(date);
  });
}

function weekRangeLabel(start: string): string {
  const days = datesInWeek(start);
  const format = (value: string) => value.slice(5).replace("-", "/");
  return format(days[0]) + "–" + format(days[6]);
}

function ReportLoadingState() {
  return (
    <section className="report-overview-flow" aria-live="polite" aria-busy="true">
      <div className="retro-frame face-report-card report-face-panel is-loading">
        <div className="report-section-heading">
          <strong>區域紀錄</strong>
          <span>正在整理紀錄</span>
        </div>
        <div className="report-zone-inline">
          <div><span>主要膚況</span><strong>讀取中</strong></div>
          <div><span>紀錄</span><strong>—</strong></div>
        </div>
        <div className="report-region-switches is-loading" aria-hidden="true">
          <button type="button" disabled>T 字部位</button>
          <button type="button" disabled>左臉頰</button>
          <button type="button" disabled>右臉頰</button>
          <button type="button" disabled>下巴／嘴周</button>
        </div>
      </div>
    </section>
  );
}

export default function SkinReportPage() {
  const router = useRouter();
  const [selectedRegion, setSelectedRegion] = useState<FaceRegion>("t_zone");
  const [selectedDate, setSelectedDate] = useState("");
  const [payload, setPayload] = useState<SkinReportResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<ReportErrorInfo | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    void getCurrentAuthIdentity()
      .then((identity) => {
        if (!active) return null;
        if (!identity) {
          router.replace("/login?next=%2Fskin-report");
          return null;
        }
        return getCurrentSkinReport(35, undefined, identity.userId);
      })
      .then((result) => {
        if (!result) return;
        if (!active) return;
        setPayload(result);
        if (result.report?.worst_zone) setSelectedRegion(result.report.worst_zone);
      })
      .catch((reason) => {
        if (active) setError(reportErrorMessage(reason));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [reloadKey, router]);

  const availableDates = useMemo(
    () => Array.from(new Set(payload?.records.map((record) => record.date) ?? [])).sort().reverse(),
    [payload]
  );
  const availableWeeks = useMemo(
    () => Array.from(new Set(availableDates.map(startOfWeek))).sort().reverse(),
    [availableDates]
  );

  useEffect(() => {
    if (!selectedDate && availableDates[0]) setSelectedDate(availableDates[0]);
  }, [availableDates, selectedDate]);

  useEffect(() => {
    if (!payload || !selectedDate) return;
    const firstRecordedRegion = payload.records.find((record) => record.date === selectedDate)?.zone;
    if (firstRecordedRegion) setSelectedRegion(firstRecordedRegion as FaceRegion);
  }, [payload, selectedDate]);

  const datedPayload = useMemo(() => {
    if (!payload || !selectedDate) return payload;
    return {
      ...payload,
      records: payload.records.filter((record) => record.date === selectedDate)
    };
  }, [payload, selectedDate]);

  const summaries = useMemo(
    () => (datedPayload ? buildRegionSummaries(datedPayload) : []),
    [datedPayload]
  );
  const selectedMapTags = useMemo(
    () => buildRegionMapTags(datedPayload?.records ?? [], selectedRegion, 4),
    [datedPayload, selectedRegion]
  );
  const selectedSummary =
    summaries.find((summary) => summary.region === selectedRegion) ?? summaries[0];
  const selectedRegionRecordCount =
    datedPayload?.records.filter((record) => record.zone === selectedRegion).length ?? 0;

  const selectedWeek = selectedDate ? startOfWeek(selectedDate) : availableWeeks[0] ?? "";
  const weekDates = useMemo(
    () => (selectedWeek ? datesInWeek(selectedWeek) : []),
    [selectedWeek]
  );
  const weekIndex = availableWeeks.indexOf(selectedWeek);
  const olderWeek = weekIndex >= 0 ? availableWeeks[weekIndex + 1] : undefined;
  const newerWeek = weekIndex > 0 ? availableWeeks[weekIndex - 1] : undefined;

  function selectWeek(weekStart: string) {
    const recordedDates = availableDates.filter((date) => startOfWeek(date) === weekStart);
    setSelectedDate(recordedDates[0] ?? weekStart);
  }

  return (
    <div className="content-wrap report-page report-ios-diary">
      <nav className="skin-workspace-tabs skin-workspace-tabs-wide" aria-label="膚況紀錄頁面切換">
        <Link className="skin-workspace-tab" href="/skin-record">
          <Icon name="edit" />
          <span>新增紀錄</span>
        </Link>
        <Link className="skin-workspace-tab is-active" href="/skin-report" aria-current="page">
          <Icon name="chart" />
          <span>分析報告</span>
        </Link>
      </nav>

      {payload && availableDates.length > 0 ? (
        <section className="report-week-diary" aria-label="每週膚況日記">
          <div className="report-week-heading">
            <button
              type="button"
              aria-label="上一週"
              disabled={!olderWeek}
              onClick={() => olderWeek && selectWeek(olderWeek)}
            >
              <span aria-hidden="true">‹</span>
            </button>
            <div>
              <span>膚況週記</span>
              <strong>{weekRangeLabel(selectedWeek)}</strong>
            </div>
            <button
              type="button"
              aria-label="下一週"
              disabled={!newerWeek}
              onClick={() => newerWeek && selectWeek(newerWeek)}
            >
              <span aria-hidden="true">›</span>
            </button>
          </div>
          <div className="report-week-days" role="tablist" aria-label="本週日期">
            {weekDates.map((date) => {
              const value = new Date(date + "T00:00:00");
              const hasRecord = availableDates.includes(date);
              const isActive = selectedDate === date;
              return (
                <button
                  key={date}
                  type="button"
                  role="tab"
                  aria-selected={isActive}
                  aria-label={date + (hasRecord ? " 有紀錄" : " 無紀錄")}
                  disabled={!hasRecord}
                  className={(isActive ? "active " : "") + (hasRecord ? "has-record" : "")}
                  onClick={() => setSelectedDate(date)}
                >
                  <span>{WEEKDAY_SHORT[value.getDay()]}</span>
                  <strong>{value.getDate()}</strong>
                  <i aria-hidden="true" />
                </button>
              );
            })}
          </div>
        </section>
      ) : null}

      {loading && !payload ? (
        <ReportLoadingState />
      ) : error ? (
        <section className="retro-frame report-summary-panel" role="alert">
          <h2>{error.title}</h2>
          <p>{error.message}</p>
          <div className="report-status-actions">
            {error.status === 401 || error.status === 403 ? (
              <Link className="glossy-primary" href="/login">前往登入</Link>
            ) : (
              <button className="glossy-primary" type="button" onClick={() => setReloadKey((value) => value + 1)}>
                重新整理報告
              </button>
            )}
          </div>
        </section>
      ) : payload && selectedSummary ? (
        <>
          <section className="report-overview-flow">
            <article className="retro-frame face-report-card report-face-panel">
              <div className="report-section-heading">
                <strong>區域紀錄</strong>
                <span>{selectedSummary.name}</span>
              </div>
              <div className="report-zone-inline" aria-live="polite">
                <div>
                  <span>主要膚況</span>
                  <strong>{selectedMapTags.length > 0 ? selectedMapTags.map((tag) => tag.displayName).join("、") : "本日無新增"}</strong>
                </div>
                <div>
                  <span>紀錄</span>
                  <strong>{selectedRegionRecordCount + " 筆"}</strong>
                </div>
              </div>

              <div className="report-region-switches" aria-label="切換臉部區域">
                {summaries.map((summary) => (
                  <button
                    key={summary.region}
                    type="button"
                    className={selectedRegion === summary.region ? "active" : ""}
                    aria-pressed={selectedRegion === summary.region}
                    onClick={() => setSelectedRegion(summary.region)}
                  >
                    {summary.name}
                  </button>
                ))}
              </div>
            </article>


          </section>

          <nav className="report-destination-grid report-action-row" aria-label="膚況報告功能">
            <Link className="report-destination-button" href={"/skin-report/history?date=" + selectedDate}>
              <span className="report-destination-icon"><Icon name="calendar" /></span>
              <strong>完整膚況紀錄</strong>
              <Icon name="arrow" />
            </Link>
            <Link className="report-destination-button is-care" href={"/skin-report/care?date=" + selectedDate}>
              <span className="report-destination-icon"><Icon name="chart" /></span>
              <strong>膚況保養建議</strong>
              <Icon name="arrow" />
            </Link>
          </nav>
        </>
      ) : payload && selectedDate ? (
        <section className="report-empty-date" role="status">
          <strong>這一天沒有膚況紀錄</strong>
          <p>請選擇週曆中有圓點的日期。</p>
        </section>
      ) : null}

    </div>
  );
}
