export type RootRoute =
  | "product_search"
  | "skin_record"
  | "skin_consult"
  | "clarify";

export type FaceRegion = "t_zone" | "left_cheek" | "right_cheek" | "chin";

export type SkinCondition =
  | "dryness"
  | "oiliness"
  | "tightness"
  | "redness_feeling"
  | "roughness"
  | "sticky_feeling"
  | "stable";

export interface QuickReply {
  label: string;
  value: string;
  action?: string;
}

export interface ChatCardField {
  label: string;
  value: string;
}

export interface ChoiceChatCard {
  type: "choice";
  title: string;
  options: QuickReply[];
}

export interface SummaryChatCard {
  type: "summary";
  title: string;
  fields: ChatCardField[];
  actions: QuickReply[];
}

export type ChatCard = ChoiceChatCard | SummaryChatCard;

export interface ConversationLimitStatus {
  blocked: boolean;
  warning: boolean;
  reason: "messages" | "tokens" | null;
  user_messages: number;
  total_messages: number;
  used_tokens: number;
  max_user_messages: number;
  max_tokens: number;
  remaining_user_messages: number;
  remaining_tokens: number;
  warning_percent: number;
  message: string;
}

export interface RecommendationProductEvidence {
  id?: string;
  name: string;
  brand?: string;
  price?: number;
  currency?: string;
  product_version?: string;
  capacity?: string;
  personalization_policy_version?: string;
  personalization_reasons?: string[];
  personalization_missing?: string[];
  recommendation_limitations?: string;
  inci_status?: string;
  ingredient_evidence_level?: string;
  formula_market?: string;
  inci_source_url?: string;
  texture?: string;
  texture_source_url?: string;
  texture_verification_status?: string;
  price_observed_at?: string;
  price_source_url?: string;
  source_title?: string;
  source_url?: string;
  evidence_status?: string;
  evidence_updated_at?: string;
}

export interface RootAgentResponse {
  route: RootRoute;
  reply: string;
  sessionId: string;
  products?: RecommendationProductEvidence[];
  quick_replies?: QuickReply[];
  cards?: ChatCard[];
  conversation_limit?: ConversationLimitStatus;
}

export interface ProfileForm {
  name: string;
  gender: string;
  birthday: string;
  email: string;
  lineBound: boolean;
  skinType: string;
  allergens: string[];
  otherAllergen: string;
  customerType: string;
  wellnessStatus: string;
  periodStartDate: string;
  periodEndDate: string;
}

export interface SkinRecordPayload {
  recordDate: string;
  region: FaceRegion;
  conditions: SkinCondition[];
  severity: number;
  notes: string;
}
