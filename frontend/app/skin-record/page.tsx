"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { type TouchEvent, useEffect, useRef, useState } from "react";

import { FaceMap } from "@/components/FaceMap";
import { Icon } from "@/components/Icon";
import { getCurrentAuthIdentity } from "@/lib/auth-client";
import {
  createSkinRecord,
  sendSkinTrackingMessage,
  SkinTrackingApiError
} from "@/lib/skin-tracking-api";
import type { FaceRegion, SkinTrackingQuickReply } from "@/types/skin-tracking";

type ConversationItem = {
  id: number;
  role: "user" | "assistant";
  text: string;
};

type EntryStage = "location" | "issue" | "severity" | "ready";
type SaveState = "idle" | "saved" | "error";
type FacePoint = { label: string; x: number; y: number };

const REGION_LABELS: Record<FaceRegion, string> = {
  t_zone: "T 字部位",
  left_cheek: "左臉頰",
  right_cheek: "右臉頰",
  chin: "下巴／嘴周"
};

const REGION_ORDER = Object.keys(REGION_LABELS) as FaceRegion[];

function locationKey(region: FaceRegion, label: string) {
  return region + "::" + label;
}

function locationParts(value: string) {
  const separator = value.indexOf("::");
  return { region: value.slice(0, separator) as FaceRegion, label: value.slice(separator + 2) };
}

const FACE_POINTS: Record<FaceRegion, FacePoint[]> = {
  t_zone: [
    { label: "額頭", x: 50, y: 48 },
    { label: "眉心", x: 50, y: 59 },
    { label: "鼻樑", x: 50, y: 65 },
    { label: "鼻翼", x: 43, y: 70 },
    { label: "鼻頭", x: 50, y: 72 }
  ],
  left_cheek: [
    { label: "左眼周", x: 37, y: 62 },
    { label: "左顴骨", x: 35, y: 70 },
    { label: "左臉頰", x: 36, y: 76 }
  ],
  right_cheek: [
    { label: "右眼周", x: 63, y: 62 },
    { label: "右顴骨", x: 65, y: 70 },
    { label: "右臉頰", x: 64, y: 76 }
  ],
  chin: [
    { label: "人中", x: 50, y: 75 },
    { label: "嘴角", x: 57, y: 78 },
    { label: "下唇周圍", x: 50, y: 80 },
    { label: "下巴", x: 50, y: 85 }
  ]
};

const ISSUE_OPTIONS = ["痘痘", "乾燥", "泛紅", "出油", "搔癢", "斑點／痘疤", "其他"];
const ISSUE_SUGGESTIONS: Record<FaceRegion, string[]> = {
  t_zone: ["出油", "痘痘", "乾燥", "斑點／痘疤"],
  left_cheek: ["泛紅", "乾燥", "斑點／痘疤", "搔癢"],
  right_cheek: ["泛紅", "乾燥", "斑點／痘疤", "搔癢"],
  chin: ["痘痘", "乾燥", "泛紅", "搔癢"]
};

const ISSUE_RULES: Array<{ label: string; words: string[] }> = [
  { label: "痘痘", words: ["痘", "粉刺", "閉鎖", "冒包", "丘疹"] },
  { label: "乾燥", words: ["乾", "脫皮", "緊繃", "粗糙", "缺水"] },
  { label: "泛紅", words: ["紅", "泛紅", "敏感", "灼熱", "紅腫"] },
  { label: "出油", words: ["油", "油光", "出油", "黏膩"] },
  { label: "搔癢", words: ["癢", "搔癢", "發癢", "刺癢"] },
  { label: "斑點／痘疤", words: ["斑", "痘疤", "色素", "暗沉", "痘印", "黑點", "變深", "色塊"] }
];

function makeSessionId(prefix = "skin"): string {
  if (typeof window !== "undefined" && window.crypto?.randomUUID) {
    return prefix + "-" + window.crypto.randomUUID();
  }
  return prefix + "-" + Date.now().toString(36);
}

function localDateValue(date = new Date()): string {
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 10);
}

function shiftDate(value: string, amount: number): string {
  const date = new Date(value + "T12:00:00");
  date.setDate(date.getDate() + amount);
  return localDateValue(date);
}

function dateParts(value: string) {
  const date = new Date(value + "T12:00:00");
  const weekdays = ["週日", "週一", "週二", "週三", "週四", "週五", "週六"];
  const monthShort = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
  return {
    month: date.getMonth() + 1 + "月",
    monthShort: monthShort[date.getMonth()],
    day: String(date.getDate()).padStart(2, "0"),
    weekday: weekdays[date.getDay()]
  };
}

function browserErrorMessage(error: unknown): string {
  if (!(error instanceof SkinTrackingApiError)) return "目前無法連線，請稍後再試一次。";
  if (error.status === 410) return "這次對話已過期，請重新開始。";
  if (error.status === 422) return "內容無法處理，請換一種說法。";
  if (error.status === 503) return "膚況服務暫時忙碌，請稍後再試。";
  return "目前無法完成膚況紀錄，請稍後再試一次。";
}

function normalizeText(value: string): string {
  return value.toLowerCase().replace(/[\s、，。,.!！?？/／_-]/g, "");
}

function classifyIssueLocally(value: string): string | null {
  const clean = normalizeText(value);
  const exact = ISSUE_OPTIONS.find((label) => normalizeText(label) === clean);
  if (exact) return exact;
  return ISSUE_RULES.find((rule) => rule.words.some((word) => clean.includes(normalizeText(word))))?.label || null;
}

function findIssueInReply(value: string): string | null {
  const clean = normalizeText(value);
  return ISSUE_OPTIONS.find((label) => clean.includes(normalizeText(label))) || null;
}

const DEFAULT_SEVERITY_DESCRIPTIONS = [
  "偶爾出現，近看才明顯。",
  "局部可見，日常感受不強。",
  "持續可見，需要留意變化。",
  "範圍或不適感較明顯。",
  "反覆或強烈，建議尋求專業評估。"
];

const ISSUE_SEVERITY_DESCRIPTIONS: Record<string, string[]> = {
  痘痘: [
    "偶爾一兩顆，近看才明顯。",
    "局部有少量痘痘或粉刺，沒有明顯不適。",
    "數量增加或反覆出現，需要持續觀察。",
    "多處明顯，並伴隨紅腫或疼痛。",
    "範圍廣、持續惡化或有明顯疼痛。"
  ],
  乾燥: [
    "偶爾有些緊繃，外觀不明顯。",
    "局部乾燥或輕微粗糙，日常影響不大。",
    "持續緊繃，並有局部脫皮。",
    "多處脫皮、粗糙或有明顯不適。",
    "廣泛乾裂、刺痛或持續惡化。"
  ],
  泛紅: [
    "短暫泛紅，休息後會慢慢退去。",
    "局部可見泛紅，沒有明顯灼熱。",
    "泛紅持續可見，偶爾伴隨熱感。",
    "範圍擴大，並有明顯灼熱或敏感。",
    "持續紅腫、疼痛或快速惡化。"
  ],
  出油: [
    "只有些微油光，觸感仍清爽。",
    "局部油光明顯，但不太影響妝感。",
    "數小時內明顯出油，需要補妝或清潔。",
    "大範圍油膩，並容易堵塞毛孔。",
    "持續大量出油，伴隨明顯粉刺或發炎。"
  ],
  搔癢: [
    "偶爾輕癢，很快就能緩解。",
    "局部發癢，但不影響日常活動。",
    "反覆搔癢，會忍不住碰觸。",
    "搔癢明顯，並影響專注或睡眠。",
    "強烈持續搔癢，伴隨破皮或紅腫。"
  ],
  "斑點／痘疤": [
    "近看才看得見，顏色很淡。",
    "局部可見淡色斑點或痘印。",
    "多處可見，色差開始明顯。",
    "顏色較深，或範圍持續擴大。",
    "色差非常明顯，或短期快速變化。"
  ]
};

function severityMeta(percent: number, issue: string, useDefault = false) {
  const level = Math.min(4, Math.max(0, Math.ceil(percent / 20) - 1));
  const labels = ["輕微", "輕度", "中等", "明顯", "嚴重"];
  const descriptions = useDefault
    ? DEFAULT_SEVERITY_DESCRIPTIONS
    : ISSUE_SEVERITY_DESCRIPTIONS[issue] || DEFAULT_SEVERITY_DESCRIPTIONS;
  return { label: labels[level], description: descriptions[level] };
}

function severityTone(percent: number) {
  if (percent <= 20) return "#b5a4bc";
  if (percent <= 40) return "#9c85a6";
  if (percent <= 60) return "#846a8d";
  if (percent <= 80) return "#6e536f";
  return "#543e55";
}

export default function SkinRecordPage() {
  const router = useRouter();
  const nextMessageId = useRef(0);
  const locationCarouselRef = useRef<HTMLDivElement>(null);
  const regionRailTouchStartRef = useRef<{ x: number; y: number } | null>(null);
  const [sessionId, setSessionId] = useState("");
  const [classificationSessionId, setClassificationSessionId] = useState("");
  const [recordDate, setRecordDate] = useState("");
  const [selectedRegion, setSelectedRegion] = useState<FaceRegion>("t_zone");
  const [selectedRegions, setSelectedRegions] = useState<FaceRegion[]>(["t_zone"]);
  const [selectedSubregions, setSelectedSubregions] = useState<string[]>([]);
  const [customLocationOpen, setCustomLocationOpen] = useState(false);
  const [issue, setIssue] = useState("");
  const [issueUsesDefaultSeverity, setIssueUsesDefaultSeverity] = useState(false);
  const [severityPercent, setSeverityPercent] = useState(45);
  const [stage, setStage] = useState<EntryStage>("location");
  const [naturalInput, setNaturalInput] = useState("");
  const [mappingBusy, setMappingBusy] = useState(false);
  const [mappingNotice, setMappingNotice] = useState("");
  const [note, setNote] = useState("");
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [statusMessage, setStatusMessage] = useState("");
  const [assistantOpen, setAssistantOpen] = useState(false);
  const [chatInput, setChatInput] = useState("");
  const [messages, setMessages] = useState<ConversationItem[]>([]);
  const [quickReplies, setQuickReplies] = useState<SkinTrackingQuickReply[]>([]);
  const [loadingMode, setLoadingMode] = useState<"direct" | "chat" | null>(null);
  const [authState, setAuthState] = useState<"checking" | "ready" | "error">("checking");

  useEffect(() => {
    let cancelled = false;

    void getCurrentAuthIdentity()
      .then((identity) => {
        if (cancelled) return;
        if (!identity?.userId) {
          router.replace("/login?next=%2Fskin-record");
          return;
        }
        setAuthState("ready");
      })
      .catch((error) => {
        console.error("Failed to resolve skin record member session:", error);
        if (!cancelled) setAuthState("error");
      });

    return () => {
      cancelled = true;
    };
  }, [router]);

  useEffect(() => {
    if (authState !== "ready") return;
    const params = new URLSearchParams(window.location.search);
    const activeSessionId = params.get("session_id")?.trim() || makeSessionId();
    setSessionId(activeSessionId);
    setClassificationSessionId(makeSessionId("classify"));
    setRecordDate(localDateValue());
    params.set("session_id", activeSessionId);
    params.delete("message");
    window.history.replaceState(null, "", window.location.pathname + "?" + params.toString());
  }, [authState]);

  useEffect(() => {
    const carousel = locationCarouselRef.current;
    if (!carousel || stage !== "location") return;
    const index = REGION_ORDER.indexOf(selectedRegion);
    const targetLeft = carousel.clientWidth * index;
    carousel.style.scrollBehavior = "auto";
    carousel.scrollLeft = targetLeft;
    const frame = window.requestAnimationFrame(() => {
      carousel.style.scrollBehavior = "";
    });
    return () => window.cancelAnimationFrame(frame);
  }, [selectedRegion, stage]);

  const todayDate = localDateValue();
  const visibleRecordDate = recordDate || todayDate;
  const currentDate = dateParts(visibleRecordDate);
  const recentDateOptions = Array.from({ length: 14 }, (_, index) => {
    const value = shiftDate(todayDate, -index);
    const parts = dateParts(value);
    return { value, label: parts.month + " " + parts.day + " " + parts.weekday };
  });
  const selectedLocations = selectedSubregions.map(locationParts);
  const subregion = selectedLocations.map((location) => location.label).join("、");
  const severity = severityMeta(severityPercent, issue, issueUsesDefaultSeverity);
  const severityLevel = Math.max(1, Math.min(5, Math.ceil(severityPercent / 20)));
  const severityToneColor = severityTone(severityPercent);
  const formReady = Boolean(recordDate && selectedSubregions.length > 0 && issue && sessionId && !loadingMode);

  function chooseRegion(region: FaceRegion) {
    setSelectedRegion(region);
    setSelectedRegions((current) => current.includes(region) ? current : [...current, region]);
    setIssue("");
    setSeverityPercent(45);
    setStage("location");
    setNaturalInput("");
    setMappingNotice("");
    setCustomLocationOpen(false);
    setSaveState("idle");
  }

  function handleLocationScroll() {
    const carousel = locationCarouselRef.current;
    if (!carousel || carousel.clientWidth === 0) return;
    const index = Math.max(0, Math.min(REGION_ORDER.length - 1, Math.round(carousel.scrollLeft / carousel.clientWidth)));
    const region = REGION_ORDER[index];
    if (region !== selectedRegion) setSelectedRegion(region);
  }

  function handleRegionRailTouchStart(event: TouchEvent<HTMLDivElement>) {
    const touch = event.touches[0];
    if (!touch) return;
    regionRailTouchStartRef.current = { x: touch.clientX, y: touch.clientY };
  }

  function handleRegionRailTouchEnd(event: TouchEvent<HTMLDivElement>) {
    const start = regionRailTouchStartRef.current;
    const touch = event.changedTouches[0];
    regionRailTouchStartRef.current = null;
    if (!start || !touch) return;

    const deltaX = touch.clientX - start.x;
    const deltaY = touch.clientY - start.y;
    if (Math.abs(deltaX) < 34 || Math.abs(deltaX) <= Math.abs(deltaY) * 1.15) return;

    const currentIndex = REGION_ORDER.indexOf(selectedRegion);
    const nextIndex = Math.max(0, Math.min(REGION_ORDER.length - 1, currentIndex + (deltaX < 0 ? 1 : -1)));
    if (nextIndex !== currentIndex) chooseRegion(REGION_ORDER[nextIndex]);
  }

  function chooseSubregion(region: FaceRegion, value: string) {
    const key = locationKey(region, value);
    setSelectedRegion(region);
    setSelectedRegions((current) => current.includes(region) ? current : [...current, region]);
    setSelectedSubregions((current) =>
      current.includes(key) ? current.filter((item) => item !== key) : [...current, key]
    );
    setIssue("");
    setSeverityPercent(45);
    setStage("location");
    setNaturalInput("");
    setMappingNotice("");
    setCustomLocationOpen(false);
  }

  function confirmSubregions() {
    if (selectedSubregions.length === 0) return;
    setCustomLocationOpen(false);
    setStage("issue");
    setMappingNotice("已選擇「" + selectedLocations.map((location) => location.label).join("、") + "」");
  }

  function chooseIssue(value: string, notice = "", useDefaultSeverity = false) {
    setIssue(value);
    setIssueUsesDefaultSeverity(useDefaultSeverity);
    setStage("severity");
    setNaturalInput("");
    setMappingNotice(notice);
  }

  function appendMessage(role: ConversationItem["role"], text: string) {
    nextMessageId.current += 1;
    setMessages((current) => [...current, { id: nextMessageId.current, role, text }]);
  }

  async function submitNaturalInput() {
    const clean = naturalInput.trim();
    if (!clean || mappingBusy) return;
    setMappingNotice("");

    if (stage === "location") {
      const candidates = (Object.keys(FACE_POINTS) as FaceRegion[]).flatMap((region) =>
        FACE_POINTS[region].map((point) => ({ region, label: point.label }))
      );
      const normalized = normalizeText(clean);
      const match = candidates.find((candidate) =>
        normalized.includes(normalizeText(candidate.label)) || normalizeText(candidate.label).includes(normalized)
      );
      if (match) {
        chooseSubregion(match.region, match.label);
        setCustomLocationOpen(false);
        setMappingNotice("已加入固定部位「" + match.label + "」");
      } else {
        setMappingNotice("找不到固定部位，請點臉上的定位點或推薦字卡。");
      }
      return;
    }

    if (stage !== "issue") return;
    const localMatch = classifyIssueLocally(clean);
    if (localMatch) {
      chooseIssue(localMatch, "", true);
      return;
    }

    setMappingBusy(true);
    try {
      const response = await sendSkinTrackingMessage({
        sessionId: classificationSessionId || makeSessionId("classify"),
        message: "這是分類請求，不要建立紀錄。請只把使用者描述對應成資料庫既有的一個膚況標籤：痘痘、乾燥、泛紅、出油、搔癢、斑點／痘疤、其他。描述：" + clean
      });
      const mapped = findIssueInReply(response.reply) || "其他";
      chooseIssue(mapped, "", true);
    } catch {
      chooseIssue("其他", "", true);
    } finally {
      setMappingBusy(false);
    }
  }

  async function sendAssistantMessage(message: string) {
    const cleanMessage = message.trim();
    if (!cleanMessage || !sessionId || loadingMode) return;
    appendMessage("user", cleanMessage);
    setChatInput("");
    setQuickReplies([]);
    setLoadingMode("chat");
    try {
      const response = await sendSkinTrackingMessage({ message: cleanMessage, sessionId });
      appendMessage("assistant", response.reply);
      setQuickReplies(response.quick_replies);
    } catch (error) {
      appendMessage("assistant", browserErrorMessage(error));
    } finally {
      setLoadingMode(null);
    }
  }

  async function saveDirectRecord() {
    if (!formReady) return;
    setSaveState("idle");
    setStatusMessage("");
    setLoadingMode("direct");

    try {
      for (const location of selectedLocations) {
        await createSkinRecord({
          recordDate,
          region: location.label,
          conditions: [issue],
          severity: severityLevel,
          notes: note.trim(),
          sessionId
        });
      }

      setSaveState("saved");
      setStatusMessage("已直接儲存至膚況紀錄");
    } catch (error) {
      setSaveState("error");
      setStatusMessage(browserErrorMessage(error));
    } finally {
      setLoadingMode(null);
    }
  }

  function renderLayerCard() {
    if (stage === "location") {
      return (
        <section className="record-location-carousel-shell" aria-live="polite">
          <div
            className="record-location-carousel"
            ref={locationCarouselRef}
            onScroll={handleLocationScroll}
          >
            {REGION_ORDER.map((region) => {
              const selectedInRegion = FACE_POINTS[region].filter((point) =>
                selectedSubregions.includes(locationKey(region, point.label))
              ).length;
              const isCurrent = selectedRegion === region;
              return (
                <section
                  className={"record-layer-card is-location record-location-slide" + (isCurrent ? " is-current" : "")}
                  key={"location-" + region}
                  aria-label={REGION_LABELS[region] + "精確位置"}
                >
                  <div className="record-layer-options is-multiple">
                    {FACE_POINTS[region].map((point) => {
                      const key = locationKey(region, point.label);
                      const isSelected = selectedSubregions.includes(key);
                      return (
                        <button
                          type="button"
                          key={point.label}
                          className={isSelected ? "active" : ""}
                          aria-pressed={isSelected}
                          onClick={() => chooseSubregion(region, point.label)}
                        >
                          {point.label}
                        </button>
                      );
                    })}
                    <button
                      type="button"
                      className={"record-custom-location-toggle" + (customLocationOpen && isCurrent ? " active" : "")}
                      aria-expanded={customLocationOpen && isCurrent}
                      onClick={() => {
                        setSelectedRegion(region);
                        setSelectedRegions((current) => current.includes(region) ? current : [...current, region]);
                        setCustomLocationOpen((current) => !current || !isCurrent);
                        setNaturalInput("");
                        setMappingNotice("");
                      }}
                    >
                      <span aria-hidden="true">＋</span> 自訂
                    </button>
                  </div>
                  {customLocationOpen && isCurrent ? (
                    <div className="record-custom-location">
                      <input
                        value={naturalInput}
                        onChange={(event) => setNaturalInput(Array.from(event.target.value).slice(0, 5).join(""))}
                        onKeyDown={(event) => {
                          if (event.key === "Enter" && !event.nativeEvent.isComposing) void submitNaturalInput();
                        }}
                        placeholder="最多五個字"
                        aria-label="自訂精確位置，最多五個字"
                        maxLength={5}
                        autoFocus
                      />
                      <button type="button" aria-label="加入自訂部位" disabled={!naturalInput.trim()} onClick={() => void submitNaturalInput()}>＋</button>
                    </div>
                  ) : null}
                  {mappingNotice && isCurrent ? <p className={"record-custom-location-notice " + (mappingNotice.includes("找不到") || mappingNotice.includes("無法") ? "is-warning" : "is-positive")}>{mappingNotice}</p> : null}
                  <div className="record-multi-footer">
                    <span className={selectedInRegion > 0 ? "is-selected" : "is-pending"}>{selectedInRegion > 0 ? "此區已選 " + selectedInRegion + " 個部位" : "尚未選擇"}</span>
                    <button type="button" disabled={selectedSubregions.length === 0} onClick={confirmSubregions}>下一步</button>
                  </div>
                </section>
              );
            })}
          </div>
          <div className="record-location-dots" aria-label="精確位置頁面">
            {REGION_ORDER.map((region) => (
              <button
                type="button"
                key={"dot-" + region}
                className={selectedRegion === region ? "active" : ""}
                aria-label={"切換到" + REGION_LABELS[region]}
                aria-current={selectedRegion === region ? "page" : undefined}
                onClick={() => chooseRegion(region)}
              />
            ))}
          </div>
        </section>
      );
    }

    if (stage === "issue") {
      return (
        <section className="record-layer-card" key={"issue-" + selectedRegion + "-" + subregion} aria-live="polite">
          <header>
            <div><span>02</span><strong>{subregion}常見膚況</strong></div>
            <button type="button" className="record-layer-back" onClick={() => setStage("location")}>重選位置</button>
          </header>
          <p>先顯示較常見的固定標籤，也可以用自己的話描述。</p>
          <div className="record-layer-options is-issues">
            {ISSUE_SUGGESTIONS[selectedRegion].map((item) => (
              <button type="button" key={item} onClick={() => chooseIssue(item)}>{item}</button>
            ))}
          </div>
          <div className="record-smart-input">
            <input
              value={naturalInput}
              onChange={(event) => setNaturalInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.nativeEvent.isComposing) void submitNaturalInput();
              }}
              placeholder="例如：緊繃、冒粉刺、摸起來粗粗的"
              aria-label="用自然語言描述膚況"
              maxLength={80}
            />
            <button type="button" disabled={!naturalInput.trim() || mappingBusy} onClick={() => void submitNaturalInput()} aria-label="智慧對應膚況標籤">
              {mappingBusy ? "•••" : "↗"}
            </button>
          </div>
          {mappingNotice ? <p className={"record-mapping-notice " + (mappingNotice.includes("找不到") || mappingNotice.includes("無法") ? "is-warning" : "is-positive")}>{mappingNotice}</p> : null}
        </section>
      );
    }

    if (stage === "severity") {
      return (
        <section className="record-layer-card is-severity" key={"severity-" + issue} aria-live="polite">
          <header>
            <div><span>03</span><strong>{issue}的明顯程度</strong></div>
            <button type="button" className="record-layer-back" onClick={() => setStage("issue")}>重選膚況</button>
          </header>
          <div className="record-severity-readout">
            <strong style={{ color: severityToneColor }}>{severityPercent}<small>%</small></strong>
            <div><b>{severity.label}</b><p>{severity.description}</p></div>
          </div>
          <input
            className="record-severity-slider"
            type="range"
            min="1"
            max="100"
            value={severityPercent}
            onChange={(event) => setSeverityPercent(Number(event.target.value))}
            aria-label="膚況明顯程度百分比"
            style={{ background: "linear-gradient(90deg, " + severityToneColor + " 0%, " + severityToneColor + " " + severityPercent + "%, #eee8ee " + severityPercent + "%, #eee8ee 100%)" }}
          />
          <div className="record-severity-scale"><span>輕微</span><span>中等</span><span>嚴重</span></div>
          <button type="button" className="record-layer-next" style={{ background: severityToneColor }} onClick={() => setStage("ready")}>確認程度</button>
        </section>
      );
    }

    return (
      <section className="record-layer-card is-ready" key="ready" aria-live="polite">
        <header>
          <div><span>04</span><strong>確認這筆紀錄</strong></div>
          <button type="button" className="record-layer-back" onClick={() => setStage("severity")}>調整程度</button>
        </header>
        <div className="record-ready-summary">
          <span>紀錄日期</span><strong>{recordDate}</strong>
          <span>部位與膚況</span><strong>{subregion} · {issue}</strong>
          <span>明顯程度</span><b style={{ color: severityToneColor }}>{severity.label} · {severityPercent}%</b>
        </div>
        <label className="record-ready-note">
          <span>補充一句 <small>選填</small></span>
          <input value={note} onChange={(event) => setNote(event.target.value)} placeholder="例如：洗臉後比較明顯" maxLength={120} />
        </label>
        <button className="record-entry-save" type="button" disabled={!formReady || saveState === "saved"} onClick={() => void saveDirectRecord()}>
          <span>{loadingMode === "direct" ? "正在儲存…" : saveState === "saved" ? "已完成儲存" : "直接儲存紀錄"}</span>
        </button>
        {statusMessage ? <p className={"record-entry-status is-" + saveState} role="status">{statusMessage}</p> : null}
      </section>
    );
  }

  if (authState !== "ready") {
    return (
      <div className="content-wrap record-page record-layered-page">
        <section className="record-layer-card is-ready" aria-live="polite">
          <header><div><span>BLINK</span><strong>膚況紀錄</strong></div></header>
          <p>{authState === "error" ? "目前無法確認登入狀態，請檢查連線後重試。" : "正在確認登入狀態…"}</p>
          {authState === "error" ? (
            <button type="button" className="record-layer-next" onClick={() => window.location.reload()}>重新整理</button>
          ) : null}
        </section>
      </div>
    );
  }

  return (
    <div className="content-wrap record-page record-layered-page">
      <nav className="skin-workspace-tabs skin-workspace-tabs-wide" aria-label="膚況紀錄頁面切換">
        <Link className="skin-workspace-tab is-active" href="/skin-record" aria-current="page">
          <Icon name="edit" /><span>新增紀錄</span>
        </Link>
        <Link className="skin-workspace-tab" href="/skin-report">
          <Icon name="chart" /><span>分析報告</span>
        </Link>
      </nav>

      <section className="record-layered-shell" aria-label="新增膚況紀錄">
        <div className="record-visual-row">
          <div className="record-face-picker">
            <label className="record-date-square">
              <span className="record-date-binding" aria-hidden="true"><i /><i /></span>
              <span className="record-date-month">{currentDate.monthShort}</span>
              <strong>{currentDate.day}</strong>
              <select aria-label="選擇紀錄日期" value={visibleRecordDate} onChange={(event) => setRecordDate(event.target.value)}>
                {recentDateOptions.map((option) => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>

            <div className="record-entry-face-stage">
              <FaceMap selected={selectedRegion} onSelect={chooseRegion} compact />
              <div className="record-face-points" aria-label="精確臉部位置">
                {FACE_POINTS[selectedRegion].map((point) => (
                  <button
                    type="button"
                    key={point.label}
                    className={selectedSubregions.includes(locationKey(selectedRegion, point.label)) ? "active" : ""}
                    aria-label={"選擇" + point.label}
                    aria-pressed={selectedSubregions.includes(locationKey(selectedRegion, point.label))}
                    style={{ left: point.x + "%", top: point.y + "%" }}
                    onClick={() => chooseSubregion(selectedRegion, point.label)}
                  ><span /></button>
                ))}
              </div>
            </div>
            <div
              className="record-region-rail"
              aria-label="臉部區域（可複選）"
              onTouchStart={handleRegionRailTouchStart}
              onTouchEnd={handleRegionRailTouchEnd}
              onTouchCancel={() => { regionRailTouchStartRef.current = null; }}
            >
              {REGION_ORDER.map((region) => {
                const hasSelection = selectedRegions.includes(region) || selectedSubregions.some((value) => value.startsWith(region + "::"));
                return (
                  <button
                    type="button"
                    key={region}
                    className={(selectedRegion === region ? "active " : "") + (hasSelection ? "has-selection" : "")}
                    aria-pressed={hasSelection}
                    onClick={() => chooseRegion(region)}
                  >
                    {REGION_LABELS[region]}
                  </button>
                );
              })}
            </div>
          </div>
        </div>

        <div className="record-layer-stage">{renderLayerCard()}</div>
      </section>

      <section className={"record-assistant-panel record-layered-assistant" + (assistantOpen ? " is-open" : "")}>
        <button className="record-assistant-trigger" type="button" aria-expanded={assistantOpen} onClick={() => setAssistantOpen((value) => !value)}>
          <span className="record-assistant-avatar"><img src="/blink-ai-avatar.svg" alt="" /></span>
          <span><strong>想用對話完成？</strong><small>BLINK AI 也會存成相同固定格式</small></span>
          <span aria-hidden="true">{assistantOpen ? "−" : "+"}</span>
        </button>
        {assistantOpen ? (
          <div className="record-assistant-body">
            <div className="record-assistant-thread" aria-live="polite">
              {messages.length === 0 ? (
                <article className="record-assistant-message"><strong>BLINK AI</strong><p>說明位置、狀況和程度，我會整理成相同的固定分類。</p></article>
              ) : null}
              {messages.map((message) => (
                <article className={"record-assistant-message is-" + message.role} key={message.id}>
                  <strong>{message.role === "assistant" ? "BLINK AI" : "你"}</strong><p>{message.text}</p>
                </article>
              ))}
              {quickReplies.length > 0 ? (
                <div className="record-assistant-replies" aria-label="快速回覆">
                  {quickReplies.map((reply) => (
                    <button type="button" key={reply.label + "-" + reply.value} disabled={Boolean(loadingMode)} onClick={() => void sendAssistantMessage(reply.value)}>{reply.label}</button>
                  ))}
                </div>
              ) : null}
            </div>
            <div className="record-assistant-composer">
              <input
                value={chatInput}
                onChange={(event) => setChatInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.nativeEvent.isComposing) void sendAssistantMessage(chatInput);
                }}
                placeholder="例如：左臉頰泛紅，約 60%"
                aria-label="請 BLINK AI 協助整理膚況"
                maxLength={2000}
              />
              <button type="button" onClick={() => void sendAssistantMessage(chatInput)} disabled={Boolean(loadingMode) || !chatInput.trim() || !sessionId} aria-label="送出膚況描述">
                {loadingMode === "chat" ? <span>•••</span> : <Icon name="send" />}
              </button>
            </div>
          </div>
        ) : null}
      </section>
    </div>
  );
}
