"use client";

import Link from "next/link";
import { format } from "date-fns";
import type { DateRange } from "react-day-picker";
import { useEffect, useMemo, useState } from "react";

import { Icon } from "@/components/Icon";
import { OverallSkinDiary } from "@/components/OverallSkinDiary";
import { resolveCurrentUserId } from "@/lib/auth-client";
import {
  getCurrentSkinReport,
  getSkinReportRecommendations
} from "@/lib/skin-tracking-api";
import {
  buildHistoryRecords,
  buildSelectedRangeCareTips
} from "@/lib/skin-report";
import type {
  SkinRecommendationsResponse,
  SkinReportResponse
} from "@/types/skin-tracking";

export function SkinReportDetailPage({ mode }: { mode: "history" | "care" }) {
  const [payload, setPayload] = useState<SkinReportResponse | null>(null);
  const [selectedRange, setSelectedRange] = useState<DateRange | undefined>();
  const [resolvedUserId, setResolvedUserId] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [recommendations, setRecommendations] =
    useState<SkinRecommendationsResponse | null>(null);
  const [recommendationsLoading, setRecommendationsLoading] = useState(false);

  useEffect(() => {
    let active = true;
    setLoading(true);
    void resolveCurrentUserId()
      .then((userId) => {
        setResolvedUserId(userId);
        return getCurrentSkinReport(35, undefined, userId);
      })
      .then((result) => {
        if (active) setPayload(result);
      })
      .catch(() => {
        if (active) setError("目前無法載入膚況資料，請稍後再試。");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  const rangeFrom = selectedRange?.from
    ? format(selectedRange.from, "yyyy-MM-dd")
    : null;
  const rangeTo = selectedRange?.from
    ? format(selectedRange.to ?? selectedRange.from, "yyyy-MM-dd")
    : null;

  const selectedSkinRecords = useMemo(() => {
    if (!payload || !rangeFrom || !rangeTo) return [];
    return payload.records.filter(
      (record) => record.date >= rangeFrom && record.date <= rangeTo
    );
  }, [payload, rangeFrom, rangeTo]);

  const historyRecords = useMemo(
    () => buildHistoryRecords(payload?.records ?? []),
    [payload]
  );
  const careTips = useMemo(
    () => buildSelectedRangeCareTips(selectedSkinRecords),
    [selectedSkinRecords]
  );

  useEffect(() => {
    if (
      mode !== "care" ||
      !resolvedUserId ||
      !rangeFrom ||
      !rangeTo ||
      selectedSkinRecords.length === 0
    ) {
      setRecommendations(null);
      setRecommendationsLoading(false);
      return;
    }

    let active = true;
    setRecommendationsLoading(true);
    void getSkinReportRecommendations(14, 3, undefined, resolvedUserId, {
      from: rangeFrom,
      to: rangeTo
    })
      .then((result) => {
        if (active) setRecommendations(result);
      })
      .catch(() => {
        if (active) {
          setRecommendations({
            basis: `依 ${rangeFrom}～${rangeTo} 的膚況紀錄。`,
            products: [],
            excluded_ingredients: [],
            message: "商品資料暫時無法讀取，稍後重新整理即可再試。"
          });
        }
      })
      .finally(() => {
        if (active) setRecommendationsLoading(false);
      });

    return () => {
      active = false;
    };
  }, [mode, resolvedUserId, rangeFrom, rangeTo, selectedSkinRecords.length]);

  return (
    <div className="content-wrap report-page report-detail-page">
      <header className="report-subpage-nav">
        <Link href="/skin-report" aria-label="返回膚況分析總覽">
          <Icon name="arrow" />
          <span>返回總覽</span>
        </Link>
      </header>

      {loading ? (
        <section className="report-detail-state" aria-live="polite">
          <span className="report-detail-spinner" />
          <p>正在整理你的膚況資料…</p>
        </section>
      ) : error ? (
        <section className="report-detail-state" role="alert">
          <p>{error}</p>
        </section>
      ) : payload ? (
        <OverallSkinDiary
          mode={mode}
          records={historyRecords}
          careTips={careTips}
          onRangeChange={setSelectedRange}
          recommendations={recommendations}
          recommendationsLoading={recommendationsLoading}
        />
      ) : null}
    </div>
  );
}
