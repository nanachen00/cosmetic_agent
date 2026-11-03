export type FaceRegion = "t_zone" | "left_cheek" | "right_cheek" | "chin";

export type SkinIssue =
  | "acne"
  | "dryness"
  | "redness"
  | "oiliness"
  | "itching"
  | "pigmentation"
  | "acne_scar"
  | "dark_spot"
  | "other";

export interface SkinTrackingQuickReply {
  label: string;
  value: string;
}

export interface SkinTrackingResponse {
  reply: string;
  quick_replies: SkinTrackingQuickReply[];
  stage: string;
  done: boolean;
  data: {
    records?: unknown[];
    eligibility?: unknown;
    report?: unknown;
    operation?: unknown;
  } | null;
}

export interface SkinRecordTag {
  slug?: string | null;
  display_name?: string | null;
  label?: string | null;
  category?: string | null;
  subregion?: string | null;
  subregion_label?: string | null;
  detail_zone?: string | null;
  detail_zone_label?: string | null;
}

export type SkinRecordTagValue = string | SkinRecordTag;

export interface SkinRecordSubregion {
  slug?: string | null;
  display_name?: string | null;
  label?: string | null;
}

export type SkinRecordSubregionValue = string | SkinRecordSubregion;

export interface SkinTrackingRecord {
  record_id: string;
  date: string;
  issue: string | null;
  severity: number | null;
  zone: string | null;
  onset_date?: string | null;
  duration_days?: number | null;
  note?: string;
  display_note?: string | null;
  products: string[];
  tag_slugs?: string[] | null;
  tags?: SkinRecordTagValue[] | null;
  detail_zones?: SkinRecordSubregionValue[] | null;
  subregions?: SkinRecordSubregionValue[] | null;
  record_status: "recorded" | "stable";
  stability_source: "user_reported" | "agent_confirmed" | null;
}

export interface SkinReportEligibility {
  eligible: boolean;
  consecutive_days: number;
  days_needed: number;
  streak_dates: string[];
}

export interface SkinReportZone {
  zone: FaceRegion;
  record_count: number;
  top_issue: SkinIssue | null;
  avg_severity: number | null;
  max_severity: number | null;
  trend: "improving" | "stable" | "worsening" | null;
  products: string[];
}

export interface SkinReportData {
  user_id: string;
  date_from: string;
  date_to: string;
  consecutive_days: number;
  zones: SkinReportZone[];
  worst_zone: FaceRegion | null;
  summary_text: string;
}

export interface SkinReportResponse {
  eligibility: SkinReportEligibility;
  report: SkinReportData | null;
  records: SkinTrackingRecord[];
}

export interface SkinRecommendationProduct {
  id: string;
  name: string;
  brand: string;
  category: string;
  price: number | null;
  ingredients: string[];
  concerns_zh?: string[];
  reason: string;
  is_partner: boolean;
}

export interface SkinRecommendationsResponse {
  basis: string;
  products: SkinRecommendationProduct[];
  excluded_ingredients: string[];
  message: string;
}
