"use client";

import { useEffect, useMemo, useState } from "react";
import { format, parseISO } from "date-fns";

import type { FaceRegion } from "@/types";

export type HistoryObservationTag = {
  key: string;
  displayName: string;
  subregionLabel?: string;
  category?: string;
};

export type HistoryDetailRecord = {
  key: string;
  subregionLabel?: string;
  condition: string;
  severity: string;
};

export type HistoryRegionRecord = {
  region: FaceRegion;
  name: string;
  subtitle: string;
  isStable: boolean;
  issues: string[];
  details?: HistoryDetailRecord[];
};

export type SkinHistoryRecord = {
  date: string;
  weekDay: string;
  observationTags?: HistoryObservationTag[];
  regions: HistoryRegionRecord[];
};

type HistoryCardProps = {
  record: SkinHistoryRecord;
};

const REGION_ICON_PATHS: Record<FaceRegion, string> = {
  t_zone: "/skin-region-icons-v20261002/t-zone-face-v20261002.webp",
  left_cheek: "/skin-region-icons-v20261002/left-cheek-face-v20261002.webp",
  right_cheek: "/skin-region-icons-v20261002/right-cheek-face-v20261002.webp",
  chin: "/skin-region-icons-v20261002/chin-mouth-face-v20261002.webp"
};

function RegionIllustration({ region }: { region: FaceRegion }) {
  return (
    <img
      className={"history-region-illustration is-" + region}
      src={REGION_ICON_PATHS[region]}
      alt=""
      width={1254}
      height={1254}
      loading="lazy"
      decoding="async"
      aria-hidden="true"
    />
  );
}

function splitIssue(issue: string): { condition: string; severity: string } {
  const cleaned = issue
    .replace(/^\[[^\]]+\]\s*/, "")
    .replace(/[。. ]$/, "")
    .trim();
  const parts = cleaned.split(/[，,]\s*程度\s*/);
  return {
    condition: parts[0]?.trim() || "未標示",
    severity: parts[1]?.trim() || "未分級"
  };
}

function isStableRegion(region: HistoryRegionRecord): boolean {
  return region.isStable || region.issues.length === 0 || region.issues.every((issue) => /沒有|無新增|穩定/.test(issue));
}

function preferredRegion(record: SkinHistoryRecord): FaceRegion {
  return record.regions.find((region) => !isStableRegion(region))?.region ?? record.regions[0]?.region ?? "t_zone";
}

export function HistoryCard({ record }: HistoryCardProps) {
  const [activeRegion, setActiveRegion] = useState<FaceRegion>(() => preferredRegion(record));

  useEffect(() => {
    setActiveRegion(preferredRegion(record));
  }, [record.date]);

  const selectedRegion = useMemo(
    () => record.regions.find((region) => region.region === activeRegion) ?? record.regions[0],
    [activeRegion, record.regions]
  );

  if (!selectedRegion) return null;
  const isStable = isStableRegion(selectedRegion);

  return (
    <article className="history-record-card history-record-card-focused">
      <header className="history-record-heading">
        <div>
          <span>每日紀錄</span>
          <h3>
            <time dateTime={record.date}>{format(parseISO(record.date), "MM 月 dd 日")}</time>
            <small>{record.weekDay}</small>
          </h3>
        </div>
        <span className="history-record-index">{record.regions.length} 個部位</span>
      </header>

      <section className={"history-region-card history-region-focus" + (isStable ? " is-stable" : "")} aria-live="polite">
        <div className="history-region-title">
          <span className="history-region-picture">
            <RegionIllustration region={selectedRegion.region} />
          </span>
          <div>
            <small>目前查看</small>
            <h4>{selectedRegion.name}</h4>
            <p>{selectedRegion.subtitle}</p>
          </div>
          <span className="history-region-state">
            {isStable ? "無新增狀況" : selectedRegion.issues.length + " 項紀錄"}
          </span>
        </div>

        {isStable ? (
          <div className="history-stable-message">
            <strong>本日未見異常紀錄</strong>
            <p>此部位未新增需要追蹤的膚況。</p>
          </div>
        ) : (
          <div className="history-issue-list" aria-label={selectedRegion.name + "膚況紀錄"}>
            {selectedRegion.issues.map((issue) => {
              const item = splitIssue(issue);
              return (
                <div className="history-issue-row" key={issue}>
                  <span>狀況</span>
                  <strong>{item.condition}</strong>
                  <small>程度　{item.severity}</small>
                </div>
              );
            })}
          </div>
        )}
      </section>

      <p className="history-region-help">從下方切換部位，這裡會顯示該區完整紀錄。</p>

      <nav className="history-region-dock" aria-label="切換臉部區域">
        {record.regions.map((region) => (
          <button
            key={region.region}
            type="button"
            className={activeRegion === region.region ? "active" : ""}
            aria-pressed={activeRegion === region.region}
            onClick={() => setActiveRegion(region.region)}
          >
            <span className="history-region-dock-icon"><RegionIllustration region={region.region} /></span>
            <span>{region.name}</span>
          </button>
        ))}
      </nav>
    </article>
  );
}
