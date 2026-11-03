"use client";

import { useEffect, useMemo, useState } from "react";
import type { DateRange } from "react-day-picker";
import { format, parseISO } from "date-fns";

import { HistoryCareSummary } from "@/components/HistoryList";
import type { SkinHistoryRecord } from "@/components/HistoryCard";
import type { SkinRecommendationsResponse } from "@/types/skin-tracking";

type OverallSkinDiaryProps = {
  records: SkinHistoryRecord[];
  careTips: string[];
  mode?: "combined" | "history" | "care";
  onRangeChange?: (range: DateRange | undefined) => void;
  recommendations?: SkinRecommendationsResponse | null;
  recommendationsLoading?: boolean;
};

type DayEntry = {
  key: string;
  area: string;
  condition: string;
  severity: string;
};

const WEEKDAY_LABELS = ["日", "一", "二", "三", "四", "五", "六"];

function preferredDate(records: SkinHistoryRecord[]): string {
  const available = Array.from(new Set(records.map((record) => record.date))).sort().reverse();
  if (typeof window !== "undefined") {
    const requested = new URLSearchParams(window.location.search).get("date");
    if (requested && available.includes(requested)) return requested;
  }
  return available[0] ?? "";
}

function dateOptionMeta(date: string): { day: string; weekDay: string } {
  const value = parseISO(date);
  return {
    day: format(value, "MM/dd"),
    weekDay: new Intl.DateTimeFormat("zh-TW", { weekday: "short" }).format(value)
  };
}

function startOfWeek(dateValue: string): string {
  const date = parseISO(dateValue);
  date.setDate(date.getDate() - date.getDay());
  return format(date, "yyyy-MM-dd");
}

function shiftDate(dateValue: string, amount: number): string {
  const date = parseISO(dateValue);
  date.setDate(date.getDate() + amount);
  return format(date, "yyyy-MM-dd");
}

function datesForPage(pageStart: string): string[] {
  return Array.from({ length: 5 }, (_, index) => shiftDate(pageStart, index));
}

function pageRangeLabel(pageStart: string): string {
  const dates = datesForPage(pageStart);
  return format(parseISO(dates[0]), "MM/dd") + " – " + format(parseISO(dates[4]), "MM/dd");
}

function splitIssue(issue: string): { condition: string; severity: string } {
  const cleaned = issue
    .replace(/^\[[^\]]+\]\s*/, "")
    .replace(/[。.]$/, "")
    .trim();
  const parts = cleaned.split(/[，,]\s*程度\s*/);
  return {
    condition: parts[0]?.trim() || "未標示",
    severity: parts[1]?.trim() || "未分級"
  };
}

function buildDayEntries(record: SkinHistoryRecord): DayEntry[] {
  const detailedEntries = record.regions.flatMap((region) =>
    (region.details ?? []).map((detail) => ({
      key: detail.key,
      area: detail.subregionLabel?.trim() || region.name,
      condition: detail.condition,
      severity: detail.severity
    }))
  );

  if (detailedEntries.length > 0) return detailedEntries;

  return record.regions.flatMap((region) => {
    if (region.isStable) return [];
    return region.issues
      .filter((issue) => !/沒有|無新增|穩定/.test(issue))
      .map((issue, index) => {
        const item = splitIssue(issue);
        return {
          key: region.region + "-" + index,
          area: region.name,
          condition: item.condition,
          severity: item.severity
        };
      });
  });
}

function severityRank(value: string): number {
  if (/嚴重|重度/.test(value)) return 3;
  if (/中度/.test(value)) return 2;
  if (/輕度/.test(value)) return 1;
  return 0;
}

function severityPrefix(value: string): string {
  return value === "未分級" ? "" : value;
}

function entryPhrase(entry: DayEntry): string {
  return entry.area + "有" + severityPrefix(entry.severity) + entry.condition;
}

function buildDaySummary(entries: DayEntry[]): string {
  const ranked = [...entries].sort((left, right) => severityRank(right.severity) - severityRank(left.severity));
  if (ranked.length === 1) return entryPhrase(ranked[0]) + "。";

  const first = ranked[0];
  const second = ranked[1];
  const extra = ranked.length > 2 ? "，另有 " + (ranked.length - 2) + " 項紀錄" : "";
  const sameCondition = first.condition === second.condition && first.severity === second.severity;
  if (sameCondition) {
    return first.area + "與" + second.area + "都有" + severityPrefix(first.severity) + first.condition + extra + "。";
  }

  return entryPhrase(first) + "，" + second.area + "另有" + severityPrefix(second.severity) + second.condition + extra + "。";
}

function FiveDayJournalEntry({ date, record }: { date: string; record?: SkinHistoryRecord }) {
  const value = parseISO(date);
  const entries = record ? buildDayEntries(record) : [];
  const hasEntries = entries.length > 0;

  return (
    <article
      className={"five-day-journal-day " + (hasEntries ? "has-record" : "is-empty")}
      aria-label={format(value, "MM 月 dd 日") + (hasEntries ? "膚況紀錄" : "無紀錄")}
    >
      <header className="five-day-journal-date">
        <time dateTime={date}>{format(value, "M/d")}</time>
        <small>週{WEEKDAY_LABELS[value.getDay()]}</small>
        {hasEntries ? <i aria-hidden="true" /> : null}
      </header>

      {hasEntries ? (
        <div className="five-day-journal-content">
          <p className="five-day-journal-summary">{buildDaySummary(entries)}</p>
          <ul aria-label={format(value, "MM 月 dd 日") + "膚況細節"}>
            {entries.map((entry) => (
              <li key={entry.key}>
                <span>{entry.area}</span>
                <strong>{entry.condition}</strong>
                <small>{entry.severity}</small>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <div className="five-day-journal-blank" aria-hidden="true" />
      )}
    </article>
  );
}

export function OverallSkinDiary({
  records,
  careTips,
  mode = "combined",
  onRangeChange,
  recommendations = null,
  recommendationsLoading = false
}: OverallSkinDiaryProps) {
  const [activeDate, setActiveDate] = useState("");
  const [activePageStart, setActivePageStart] = useState("");
  const [dateSheetOpen, setDateSheetOpen] = useState(false);
  const isCareReport = mode === "care";
  const isHistoryReport = mode === "history";

  const availableDates = useMemo(
    () => Array.from(new Set(records.map((record) => record.date))).sort().reverse(),
    [records]
  );

  useEffect(() => {
    const nextDate = preferredDate(records);
    setActiveDate(nextDate);
    if (!nextDate) {
      setActivePageStart("");
      onRangeChange?.(undefined);
      return;
    }

    const nextPageStart = startOfWeek(nextDate);
    setActivePageStart(nextPageStart);
    if (mode === "care") {
      const value = parseISO(nextDate);
      onRangeChange?.({ from: value, to: value });
    } else {
      const pageDates = datesForPage(nextPageStart);
      onRangeChange?.({ from: parseISO(pageDates[0]), to: parseISO(pageDates[4]) });
    }
  }, [mode, records, onRangeChange]);

  const selectedRecords = useMemo(
    () => records.filter((record) => record.date === activeDate),
    [activeDate, records]
  );
  const activePageDates = useMemo(
    () => (activePageStart ? datesForPage(activePageStart) : []),
    [activePageStart]
  );
  const recordsByDate = useMemo(
    () => new Map(records.map((record) => [record.date, record])),
    [records]
  );

  const selectDate = (date: string) => {
    if (!date) return;
    setActiveDate(date);
    const value = parseISO(date);
    onRangeChange?.({ from: value, to: value });
  };

  const selectPage = (pageStart: string) => {
    setActivePageStart(pageStart);
    const pageDates = datesForPage(pageStart);
    onRangeChange?.({ from: parseISO(pageDates[0]), to: parseISO(pageDates[4]) });
  };

  const oldestDate = availableDates[availableDates.length - 1];
  const newestDate = availableDates[0];
  const pageEnd = activePageDates[4];
  const olderPage = oldestDate && activePageStart && oldestDate < activePageStart
    ? shiftDate(activePageStart, -5)
    : undefined;
  const newerPage = newestDate && pageEnd && newestDate > pageEnd
    ? shiftDate(activePageStart, 5)
    : undefined;
  const periodLabel = activeDate ? format(parseISO(activeDate), "yyyy 年 MM 月 dd 日") : "尚未選擇";
  const hasRecord = selectedRecords.length > 0;

  return (
    <section
      id="overall-skin-history"
      className={"retro-frame overall-skin-diary report-document " + (isCareReport ? "is-care-report" : "is-history-report")}
    >
      <div className="overall-diary-heading report-document-heading">
        <div>
          <h2>{isCareReport ? "膚況保養建議" : "完整膚況紀錄"}</h2>
          <p>
            {isCareReport
              ? "依所選日期整理照護重點與商品候選。"
              : "五日一頁，只列需要留意的細分部位。"}
          </p>
        </div>
      </div>

      {!isCareReport && activePageStart ? (
        <section className="five-day-journal" aria-label="五日完整膚況手帳">
          <header className="five-day-journal-toolbar">
            <button
              type="button"
              aria-label="前五日"
              disabled={!olderPage}
              onClick={() => olderPage && selectPage(olderPage)}
            >
              <span aria-hidden="true">‹</span>
            </button>
            <div>
              <strong>{pageRangeLabel(activePageStart)}</strong>
            </div>
            <button
              type="button"
              aria-label="後五日"
              disabled={!newerPage}
              onClick={() => newerPage && selectPage(newerPage)}
            >
              <span aria-hidden="true">›</span>
            </button>
          </header>

          <div className="five-day-journal-grid">
            {activePageDates.map((date) => (
              <FiveDayJournalEntry key={date} date={date} record={recordsByDate.get(date)} />
            ))}
          </div>
        </section>
      ) : null}

      {isCareReport ? (
        <section className="record-date-filter record-date-native" aria-label="選擇紀錄日期">
          <button
            className="record-date-trigger"
            type="button"
            aria-haspopup="dialog"
            aria-expanded={dateSheetOpen}
            onClick={() => setDateSheetOpen(true)}
          >
            <span>紀錄日期</span>
            <strong>{periodLabel}</strong>
            <span aria-hidden="true">⌄</span>
          </button>
        </section>
      ) : null}

      {!isHistoryReport && hasRecord ? (
        <div className="care-report-body">
          <div className="care-report-overview" aria-label="報告摘要">
            <div>
              <span>紀錄日期</span>
              <strong>{format(parseISO(activeDate), "MM/dd")}</strong>
            </div>
            <div>
              <span>有效紀錄</span>
              <strong>{selectedRecords.length} 日</strong>
            </div>
            <div>
              <span>建議類型</span>
              <strong>日常照護</strong>
            </div>
          </div>
          <HistoryCareSummary
            careTips={careTips}
            recommendations={recommendations}
            recommendationsLoading={recommendationsLoading}
          />
          <p className="care-report-disclaimer">
            本報告依個人紀錄整理，提供日常保養參考，不能取代醫師診斷；若不適持續或加劇，請諮詢皮膚科醫師。
          </p>
        </div>
      ) : null}

      {isCareReport && !hasRecord ? (
        <div className="care-report-empty">
          <strong>這一天沒有膚況紀錄</strong>
          <p>請使用上方日期按鈕選擇其他日期。</p>
        </div>
      ) : null}

      {isCareReport && dateSheetOpen ? (
        <div className="report-date-sheet-layer">
          <button
            className="report-date-sheet-backdrop"
            type="button"
            aria-label="關閉日期選擇"
            onClick={() => setDateSheetOpen(false)}
          />
          <section className="report-date-sheet" role="dialog" aria-modal="true" aria-labelledby="diary-date-sheet-title">
            <header>
              <div>
                <h2 id="diary-date-sheet-title">選擇紀錄日期</h2>
                <p>只顯示有膚況紀錄的日期</p>
              </div>
              <button type="button" aria-label="關閉" onClick={() => setDateSheetOpen(false)}>×</button>
            </header>
            <div className="report-date-sheet-list">
              {availableDates.map((date) => {
                const label = dateOptionMeta(date);
                return (
                  <button
                    key={date}
                    type="button"
                    className={activeDate === date ? "active" : ""}
                    aria-pressed={activeDate === date}
                    onClick={() => {
                      selectDate(date);
                      setDateSheetOpen(false);
                    }}
                  >
                    <span><strong>{label.day}</strong><small>{label.weekDay}</small></span>
                    <span>{activeDate === date ? "目前選擇" : "查看紀錄"}</span>
                  </button>
                );
              })}
            </div>
          </section>
        </div>
      ) : null}
    </section>
  );
}
