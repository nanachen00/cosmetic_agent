import type { HistoryObservationTag, SkinHistoryRecord } from "@/components/HistoryCard";
import type {
  FaceRegion,
  SkinRecordSubregionValue,
  SkinRecordTagValue,
  SkinReportResponse,
  SkinReportZone,
  SkinTrackingRecord,
} from "@/types/skin-tracking";

const REGION_META: Record<FaceRegion, { name: string; subtitle: string }> = {
  t_zone: { name: "T 字部位", subtitle: "額頭・眉間・鼻子" },
  left_cheek: { name: "左臉頰", subtitle: "左眼周・顴骨・臉側" },
  right_cheek: { name: "右臉頰", subtitle: "右眼周・顴骨・臉側" },
  chin: { name: "下巴／嘴周", subtitle: "人中・嘴角・下巴" }
};

const ISSUE_LABELS: Record<string, string> = {
  acne: "痘痘／粉刺",
  dryness: "乾燥／脫皮",
  redness: "泛紅",
  oiliness: "出油",
  itching: "搔癢",
  pigmentation: "斑點",
  acne_scar: "痘疤",
  dark_spot: "斑點／痘疤（舊紀錄）",
  other: "其他膚況"
};

const TREND_LABELS = {
  improving: "改善中",
  stable: "持平觀察",
  worsening: "需要注意"
} as const;

const WEEK_DAYS = ["日", "一", "二", "三", "四", "五", "六"];

export type RegionSummary = {
  region: FaceRegion;
  name: string;
  subtitle: string;
  status: string;
  statusReason: string;
  mainCondition: string;
  possibleReason: string;
  recordCount: number;
  weeklyDays: number;
  encouragement: string;
  careTips: string[];
  recentRecords: Array<{ date: string; weekDay: string; items: string[] }>;
};

function isFaceRegion(value: string | null): value is FaceRegion {
  return value !== null && value in REGION_META;
}

function severityLabel(value: number | null): string {
  if (value == null) return "";
  if (value <= 2) return "輕微";
  if (value === 3) return "中度";
  return "嚴重";
}

function noteForZone(note: string, zone: string | null): string {
  if (zone !== "left_cheek" && zone !== "right_cheek") return note;
  const sidePattern = /(?:左|右)(?:邊|側|臉頰|頰|臉|眼周|眼週)/g;
  const first = sidePattern.exec(note);
  const second = sidePattern.exec(note);
  if (!first || !second || sidePattern.exec(note) || note.slice(0, first.index).trim() || first[0][0] === second[0][0]) {
    return note;
  }
  const firstNote = note.slice(first.index, second.index).trim().replace(/[，,、；;。\s]+$/, "").trim();
  const secondNote = note.slice(second.index).trim().replace(/^[，,、；;。\s]+/, "").trim();
  if (!firstNote || !secondNote) return note;
  return first[0][0] === (zone === "left_cheek" ? "左" : "右") ? firstNote : secondNote;
}

function recordIssueLabel(record: SkinTrackingRecord): string {
  if (record.issue !== "other") return ISSUE_LABELS[record.issue ?? ""] ?? "已記錄膚況";
  const labels = (record.tags ?? []).map((tag) => {
    if (typeof tag === "string") return ISSUE_LABELS[tag] ?? "";
    const slug = tag.slug?.trim() ?? "";
    return tag.display_name?.trim() || tag.label?.trim() || ISSUE_LABELS[slug] || "";
  }).filter((label) => label && label !== "其他膚況" && label !== "其他狀況");
  if (labels.length > 0) return Array.from(new Set(labels)).join("、");
  return record.display_note?.trim() ||
    (record.note ? noteForZone(record.note, record.zone) : "") ||
    "已記錄膚況";
}

function describeRecord(record: SkinTrackingRecord): string[] {
  const issue = recordIssueLabel(record);
  const severity = record.severity === null ? "" : `，程度${severityLabel(record.severity)}`;
  const status = record.record_status === "stable" ? "（已明確確認穩定）" : "";
  const details = [`${issue}${severity}${status}`];
  const displayNote = record.display_note?.trim();
  const originalNote = record.note?.trim();
  if (displayNote) {
    if (displayNote !== issue) details.push(displayNote);
  } else if (originalNote) {
    const zoneNote = noteForZone(originalNote, record.zone);
    if (zoneNote !== issue) details.push(zoneNote);
  }
  return details;
}

function weekDay(date: string): string {
  return WEEK_DAYS[new Date(`${date}T00:00:00`).getDay()];
}

function careTip(zone: SkinReportZone): string {
  const name = REGION_META[zone.zone].name;
  if (zone.record_count === 0) return `${name}目前沒有足夠紀錄，暫不推測趨勢。`;
  if (zone.trend === "improving") return `${name}的紀錄程度正在下降，建議維持目前做法並繼續觀察。`;
  if (zone.trend === "worsening") return `${name}的紀錄程度有上升，建議先核對近期產品與生活變化。`;
  return `${name}目前趨勢持平，持續用相同尺度記錄會更容易比較。`;
}

function summaryPoints(summary: string): string[] {
  return summary
    .split(/\s*[｜\n]+\s*/)
    .map((point) => point.trim())
    .filter(Boolean);
}

function summaryLead(summary: string): string {
  const firstPoint = summaryPoints(summary)[0] ?? "";
  return firstPoint.replace(/^[^：]{1,12}：\s*/, "");
}

function conditionLabel(records: SkinTrackingRecord[], issue: string | null | undefined): string {
  if (!issue) return "目前沒有可分析的膚況紀錄";
  if (issue !== "other") return ISSUE_LABELS[issue] ?? "已記錄膚況";
  const latest = records.filter((record) => record.issue === "other")
    .sort((left, right) => right.date.localeCompare(left.date))[0];
  return latest ? recordIssueLabel(latest) : "已記錄膚況";
}

export function buildRegionSummaries(payload: SkinReportResponse): RegionSummary[] {
  const report = payload.report;  if (!report) {    return (Object.keys(REGION_META) as FaceRegion[]).map((region) => {      const meta = REGION_META[region];      const records = payload.records.filter((record) => record.zone === region);      const dates = Array.from(new Set(records.map((record) => record.date))).sort();      const recentRecords = dates.slice(-3).map((date) => ({        date: date.slice(5).replace("-", "/"),        weekDay: weekDay(date),        items: records.filter((record) => record.date === date).flatMap(describeRecord),      }));      const sortedRecords = records.slice().sort((left, right) => left.date.localeCompare(right.date));      const latest = sortedRecords[sortedRecords.length - 1];      return {        region,        name: meta.name,        subtitle: meta.subtitle,        status: records.length > 0 ? "持續觀察" : "尚無紀錄",        statusReason: records.length > 0 ? "已依目前紀錄整理" : "完成第一筆紀錄後開始分析",        mainCondition: latest ? recordIssueLabel(latest) : "尚無膚況紀錄",        possibleReason: "",        recordCount: records.length,        weeklyDays: dates.length,        encouragement: records.length > 0 ? "目前已有 " + dates.length + " 天紀錄，可持續追蹤變化。" : "目前還沒有紀錄，完成第一筆後就會顯示趨勢。",        careTips: [],        recentRecords,      };    });  }

  return report.zones.map((zone) => {
    const meta = REGION_META[zone.zone];
    const records = payload.records.filter((record) => record.zone === zone.zone);
    const dates = Array.from(new Set(records.map((record) => record.date)));
    const grouped = new Map<string, SkinTrackingRecord[]>();
    records.forEach((record) => {
      grouped.set(record.date, [...(grouped.get(record.date) ?? []), record]);
    });
    const recentRecords = Array.from(grouped.entries())
      .sort(([left], [right]) => right.localeCompare(left))
      .slice(0, 3)
      .map(([date, dailyRecords]) => ({
        date: date.slice(5).replace("-", "/"),
        weekDay: weekDay(date),
        items: dailyRecords.flatMap(describeRecord)
      }));

    return {
      region: zone.zone,
      name: meta.name,
      subtitle: meta.subtitle,
      status: zone.record_count === 0 ? "尚無紀錄" : TREND_LABELS[zone.trend ?? "stable"],
      statusReason: `共 ${zone.record_count} 筆有效紀錄，涵蓋 ${dates.length} 天。`,
      mainCondition: conditionLabel(records, zone.top_issue),
      possibleReason: "僅依你實際確認的紀錄整理，不推測未回報的原因。",
      recordCount: zone.record_count,
      weeklyDays: dates.length,
      encouragement: summaryLead(report.summary_text) || `已完成連續 ${report.consecutive_days} 天紀錄。`,
      careTips: [careTip(zone)],
      recentRecords
    };
  });
}

const DETAIL_ZONE_LABELS: Record<string, string> = {
  eye_area: "眼周",
  under_eye: "眼下",
  forehead: "額頭",
  nose: "鼻部",
  left_cheek: "左臉頰",
  right_cheek: "右臉頰",
  cheek: "臉頰",
  chin: "下巴",
  mouth_area: "嘴周",
};

function readableSlug(value: string): string {
  const normalized = value.trim();
  if (!normalized) return "";
  return normalized
    .replace(/[_-]+/g, " ")
    .replace(/w/g, (letter) => letter.toUpperCase());
}

function observationTagName(tag: SkinRecordTagValue): string {
  if (typeof tag === "string") {
    const slug = tag.trim();
    return ISSUE_LABELS[slug] ?? readableSlug(slug);
  }

  const slug = tag.slug?.trim() ?? "";
  return (
    tag.display_name?.trim() ||
    tag.label?.trim() ||
    ISSUE_LABELS[slug] ||
    readableSlug(slug)
  );
}

function subregionName(
  subregion: SkinRecordSubregionValue | null | undefined,
): string | undefined {
  if (!subregion) return undefined;
  if (typeof subregion === "string") {
    const slug = subregion.trim();
    return (DETAIL_ZONE_LABELS[slug] ?? readableSlug(slug)) || undefined;
  }

  const slug = subregion.slug?.trim() ?? "";
  return (
    subregion.display_name?.trim() ||
    subregion.label?.trim() ||
    DETAIL_ZONE_LABELS[slug] ||
    readableSlug(slug) ||
    undefined
  );
}

function buildObservationTags(
  records: SkinTrackingRecord[],
): HistoryObservationTag[] {
  const tags: HistoryObservationTag[] = [];
  const seen = new Set<string>();

  records.forEach((record) => {
    const rawTags = record.tags?.length ? record.tags : record.tag_slugs ?? [];
    const rawSubregions = record.subregions?.length
      ? record.subregions
      : record.detail_zones ?? [];

    rawTags.forEach((tag, index) => {
      const displayName = observationTagName(tag);
      if (!displayName) return;

      const inlineSubregion =
        typeof tag === "string"
          ? undefined
          : tag.subregion_label ||
            tag.detail_zone_label ||
            tag.subregion ||
            tag.detail_zone;
      const indexedSubregion =
        rawSubregions[index] ??
        (rawSubregions.length === 1 ? rawSubregions[0] : undefined);
      const subregionLabel = subregionName(
        inlineSubregion || indexedSubregion,
      );
      const category =
        typeof tag === "string" ? undefined : tag.category?.trim() || undefined;
      const dedupeKey = `${subregionLabel ?? ""}|${displayName}`.toLocaleLowerCase();
      if (seen.has(dedupeKey)) return;
      seen.add(dedupeKey);

      tags.push({
        key: `${record.record_id}:${dedupeKey}`,
        displayName,
        subregionLabel,
        category,
      });
    });
  });

  return tags;
}

export function buildRegionMapTags(
  records: SkinTrackingRecord[],
  region: FaceRegion,
  limit = 4,
): HistoryObservationTag[] {
  const tags: HistoryObservationTag[] = [];
  const seen = new Set<string>();
  const scopedRecords = records
    .filter((record) => record.zone === region)
    .slice()
    .sort(
      (left, right) =>
        right.date.localeCompare(left.date) ||
        right.record_id.localeCompare(left.record_id),
    );

  const append = (tag: HistoryObservationTag) => {
    const dedupeKey = `${tag.subregionLabel ?? ""}|${tag.displayName}`.toLocaleLowerCase();
    if (seen.has(dedupeKey)) return;
    seen.add(dedupeKey);
    tags.push(tag);
  };

  scopedRecords.forEach((record) => {
    const structuredTags = buildObservationTags([record]);
    if (structuredTags.length > 0) {
      structuredTags.forEach(append);
      return;
    }

    const displayName = recordIssueLabel(record);
    const rawSubregions = record.subregions?.length
      ? record.subregions
      : record.detail_zones ?? [];
    const subregionLabels = Array.from(
      new Set(
        rawSubregions
          .map((subregion) => subregionName(subregion))
          .filter((label): label is string => Boolean(label)),
      ),
    );

    if (subregionLabels.length === 0) {
      append({
        key: `${record.record_id}:${displayName}`,
        displayName,
      });
      return;
    }

    subregionLabels.forEach((subregionLabel) => {
      append({
        key: `${record.record_id}:${subregionLabel}:${displayName}`,
        displayName,
        subregionLabel,
      });
    });
  });

  return tags.slice(0, Math.max(0, limit));
}

export function buildHistoryRecords(records: SkinTrackingRecord[]): SkinHistoryRecord[] {
  const validRecords = records.filter((record) => isFaceRegion(record.zone));
  const dates = Array.from(new Set(validRecords.map((record) => record.date)))
    .sort();

  return dates.map((date) => ({
    date,
    weekDay: weekDay(date),
    observationTags: buildObservationTags(
      validRecords.filter((record) => record.date === date),
    ),
    regions: (Object.keys(REGION_META) as FaceRegion[]).map((region) => {
      const dailyRecords = validRecords.filter(
        (record) => record.date === date && record.zone === region
      );
      const isStable =
        dailyRecords.length > 0 &&
        dailyRecords.every((record) => record.record_status === "stable");
      const details = dailyRecords
        .filter((record) => record.record_status !== "stable")
        .flatMap((record) => {
          const condition = recordIssueLabel(record);
          const severity = severityLabel(record.severity) || "未分級";
          const rawSubregions = record.subregions?.length
            ? record.subregions
            : record.detail_zones ?? [];
          const taggedSubregionLabels = buildObservationTags([record])
            .map((tag) => tag.subregionLabel)
            .filter((label): label is string => Boolean(label));
          const subregionLabels = Array.from(
            new Set([
              ...taggedSubregionLabels,
              ...rawSubregions
                .map((subregion) => subregionName(subregion))
                .filter((label): label is string => Boolean(label)),
            ]),
          );

          if (subregionLabels.length === 0) {
            return [{
              key: record.record_id + ":" + condition,
              condition,
              severity,
            }];
          }

          return subregionLabels.map((subregionLabel) => ({
            key: record.record_id + ":" + subregionLabel + ":" + condition,
            subregionLabel,
            condition,
            severity,
          }));
        });
      return {
        region,
        ...REGION_META[region],
        isStable,
        details,
        issues: isStable
          ? []
          : dailyRecords.length > 0
            ? dailyRecords.flatMap(describeRecord)
            : ["當日沒有紀錄"]
      };
    })
  }));
}

export function buildReportCareTips(payload: SkinReportResponse): string[] {
  if (!payload.report) return [];
  if (payload.report.summary_text) return summaryPoints(payload.report.summary_text);
  return payload.report.zones.map(careTip);
}

export function buildSelectedRangeCareTips(records: SkinTrackingRecord[]): string[] {
  if (records.length === 0) return [];

  const dayCount = new Set(records.map((record) => record.date)).size;
  const active = records.filter((record) => record.record_status !== "stable");
  const concerns = Array.from(new Set(active.map((record) => {
      if (record.issue === "other") return recordIssueLabel(record);
    return ISSUE_LABELS[record.issue ?? ""] ?? "已記錄膚況";
  })));
  const focus = concerns.slice(0, 2).join("、");

  return [
    "紀錄重點：所選日期共有 " + dayCount + " 天、" + records.length + " 筆膚況紀錄" +
      (focus ? "，包含「" + focus + "」。" : "，目前沒有需要特別關注的狀況。"),
    dayCount === 1
      ? "最近的變化：目前只有一天紀錄，暫不判斷膚況變好或變嚴重。"
      : "最近的變化：這段期間有 " + dayCount + " 天紀錄；持續用相同方式記錄，才比較容易看出變化。",
    focus
      ? "先這樣照顧：先留意「" + focus + "」，維持簡單、溫和的日常保養，避免同時更換多項產品。"
      : "先這樣照顧：維持目前熟悉的日常保養，暫不依穩定紀錄推測新的問題。",
    "接下來觀察：繼續記下日期、部位與感受；若不適持續或加劇，請諮詢皮膚科醫師。"
  ];
}
