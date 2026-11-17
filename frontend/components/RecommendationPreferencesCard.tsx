"use client";

import { useEffect, useId, useMemo, useState } from "react";

import {
  getRecommendationPreferences,
  resetRecommendationPreferences,
  saveRecommendationPreferences,
  type PreferredTexture,
  type RecommendationPreferences,
} from "@/lib/preferences-api";

import styles from "./RecommendationPreferencesCard.module.css";

type BrandField = "preferred_brands" | "avoided_brands";
type Draft = {
  budget_max: string;
  preferred_texture: PreferredTexture | null;
  preferred_brands: string[];
  avoided_brands: string[];
};

const EMPTY_PREFERENCES: RecommendationPreferences = {
  budget_max: null,
  preferred_texture: null,
  preferred_brands: [],
  avoided_brands: [],
  updated_at: null,
};

const toDraft = (value: RecommendationPreferences): Draft => ({
  budget_max: value.budget_max === null ? "" : String(value.budget_max),
  preferred_texture: value.preferred_texture,
  preferred_brands: [...value.preferred_brands],
  avoided_brands: [...value.avoided_brands],
});

const normalizeBrand = (value: string): string => value.trim().replace(/\s+/g, " ");
const brandKey = (value: string): string => normalizeBrand(value).toLocaleLowerCase("zh-Hant");
const parseBrands = (value: string): string[] => value
  .split(/[、,，\n]/)
  .map(normalizeBrand)
  .filter(Boolean);

export function RecommendationPreferencesCard({
  onDirtyChange,
}: {
  onDirtyChange?: (dirty: boolean) => void;
}) {
  const id = useId();
  const [draft, setDraft] = useState<Draft>(() => toDraft(EMPTY_PREFERENCES));
  const [saved, setSaved] = useState<Draft>(() => toDraft(EMPTY_PREFERENCES));
  const [preferredBrandInput, setPreferredBrandInput] = useState("");
  const [avoidedBrandInput, setAvoidedBrandInput] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirmReset, setConfirmReset] = useState(false);

  async function loadPreferences() {
    setLoading(true);
    setError("");
    try {
      const preferences = await getRecommendationPreferences();
      const next = toDraft(preferences);
      setDraft(next);
      setSaved(next);
      setPreferredBrandInput("");
      setAvoidedBrandInput("");
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "目前無法讀取推薦偏好。");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadPreferences();
  }, []);

  const budgetValue = Number(draft.budget_max);
  const budgetInvalid = Boolean(
    draft.budget_max.trim()
    && (!Number.isFinite(budgetValue) || budgetValue < 1 || budgetValue > 100_000),
  );
  const dirty = useMemo(
    () => JSON.stringify(draft) !== JSON.stringify(saved)
      || Boolean(preferredBrandInput.trim() || avoidedBrandInput.trim()),
    [avoidedBrandInput, draft, preferredBrandInput, saved],
  );

  useEffect(() => {
    if (!dirty) return;
    const warnBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
    };
    window.addEventListener("beforeunload", warnBeforeUnload);
    return () => window.removeEventListener("beforeunload", warnBeforeUnload);
  }, [dirty]);

  useEffect(() => {
    onDirtyChange?.(dirty);
    return () => onDirtyChange?.(false);
  }, [dirty, onDirtyChange]);

  function updateDraft<K extends keyof Draft>(key: K, value: Draft[K]) {
    setDraft((current) => ({ ...current, [key]: value }));
    setNotice("");
    setError("");
    setConfirmReset(false);
  }

  function addBrands(field: BrandField, raw: string): boolean {
    const additions = parseBrands(raw);
    if (!additions.length) return true;
    const otherField: BrandField = field === "preferred_brands" ? "avoided_brands" : "preferred_brands";
    const otherKeys = new Set(draft[otherField].map(brandKey));
    if (additions.some((brand) => otherKeys.has(brandKey(brand)))) {
      setError("同一品牌不能同時設為喜歡與避用。");
      return false;
    }
    const limit = field === "preferred_brands" ? 5 : 10;
    const next = [...draft[field]];
    for (const brand of additions) {
      if (Array.from(brand).length > 40) {
        setError("品牌名稱最多 40 個字。");
        return false;
      }
      if (!next.some((item) => brandKey(item) === brandKey(brand))) next.push(brand);
    }
    if (next.length > limit) {
      setError(field === "preferred_brands" ? "最多可設定 5 個喜歡品牌。" : "最多可設定 10 個避用品牌。");
      return false;
    }
    updateDraft(field, next);
    return true;
  }

  function commitBrandInputs(): Draft | null {
    let next = { ...draft };
    const preferred = parseBrands(preferredBrandInput);
    const avoided = parseBrands(avoidedBrandInput);
    const merge = (existing: string[], additions: string[]) => {
      const result = [...existing];
      additions.forEach((brand) => {
        if (!result.some((item) => brandKey(item) === brandKey(brand))) result.push(brand);
      });
      return result;
    };
    next = {
      ...next,
      preferred_brands: merge(next.preferred_brands, preferred),
      avoided_brands: merge(next.avoided_brands, avoided),
    };
    if (next.preferred_brands.length > 5 || next.avoided_brands.length > 10) {
      setError("喜歡品牌最多 5 個，避用品牌最多 10 個。");
      return null;
    }
    if ([...preferred, ...avoided].some((brand) => Array.from(brand).length > 40)) {
      setError("品牌名稱最多 40 個字。");
      return null;
    }
    const preferredKeys = new Set(next.preferred_brands.map(brandKey));
    if (next.avoided_brands.some((brand) => preferredKeys.has(brandKey(brand)))) {
      setError("同一品牌不能同時設為喜歡與避用。");
      return null;
    }
    return next;
  }

  async function handleSave() {
    if (saving || budgetInvalid) return;
    const committed = commitBrandInputs();
    if (!committed) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const preferences = await saveRecommendationPreferences({
        budget_max: committed.budget_max.trim() ? Number(committed.budget_max) : null,
        preferred_texture: committed.preferred_texture,
        preferred_brands: committed.preferred_brands,
        avoided_brands: committed.avoided_brands,
      });
      const next = toDraft(preferences);
      setDraft(next);
      setSaved(next);
      setPreferredBrandInput("");
      setAvoidedBrandInput("");
      setNotice("推薦偏好已儲存，下一次推薦就會套用。");
    } catch (saveError) {
      setDraft(committed);
      setError(saveError instanceof Error ? saveError.message : "目前無法儲存推薦偏好。");
    } finally {
      setSaving(false);
    }
  }

  async function handleReset() {
    if (saving) return;
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const preferences = await resetRecommendationPreferences();
      const next = toDraft(preferences);
      setDraft(next);
      setSaved(next);
      setPreferredBrandInput("");
      setAvoidedBrandInput("");
      setConfirmReset(false);
      setNotice("已撤回所有推薦偏好；系統不會再沿用這些設定。");
    } catch (resetError) {
      setError(resetError instanceof Error ? resetError.message : "目前無法撤回推薦偏好。");
    } finally {
      setSaving(false);
    }
  }

  function removeBrand(field: BrandField, value: string) {
    updateDraft(field, draft[field].filter((brand) => brand !== value));
  }

  function brandEditor(field: BrandField, label: string, input: string, setInput: (value: string) => void) {
    const inputId = `${id}-${field}`;
    return (
      <div className={styles.brandField}>
        <label htmlFor={inputId}>{label}</label>
        {draft[field].length ? (
          <div className={styles.chips} aria-label={`${label}清單`}>
            {draft[field].map((brand) => (
              <button
                key={brand}
                type="button"
                className={styles.chip}
                aria-label={`移除${label}：${brand}`}
                onClick={() => removeBrand(field, brand)}
                disabled={saving}
              >
                {brand}<span aria-hidden="true">×</span>
              </button>
            ))}
          </div>
        ) : <small>尚未設定</small>}
        <div className={styles.brandComposer}>
          <input
            id={inputId}
            value={input}
            maxLength={200}
            placeholder="輸入品牌，可用逗號分隔"
            onChange={(event) => setInput(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== "Enter" && event.key !== "," && event.key !== "，") return;
              event.preventDefault();
              if (addBrands(field, input)) setInput("");
            }}
            disabled={saving}
          />
          <button
            type="button"
            className={styles.addButton}
            onClick={() => { if (addBrands(field, input)) setInput(""); }}
            disabled={saving || !input.trim()}
          >
            加入
          </button>
        </div>
      </div>
    );
  }

  return (
    <section className={`retro-frame profile-section ${styles.card}`} aria-labelledby={`${id}-title`}>
      <span className="section-tab">推薦偏好</span>
      <div className={styles.heading}>
        <div>
          <h2 id={`${id}-title`}>由你確認的選購條件</h2>
          <p>只有你主動儲存的設定會成為長期偏好；點擊商品不會自動改變它。</p>
        </div>
      </div>

      {loading ? (
        <div className={styles.state} role="status" aria-live="polite">正在讀取推薦偏好…</div>
      ) : error && !dirty ? (
        <div className={styles.state}>
          <p role="alert">{error}</p>
          <button type="button" className={styles.secondaryButton} onClick={() => void loadPreferences()}>重新讀取</button>
        </div>
      ) : (
        <div className={styles.content}>
          <label className={styles.budgetField} htmlFor={`${id}-budget`}>
            <span>單品預算上限</span>
            <span className={styles.moneyInput}><b>NT$</b><input
              id={`${id}-budget`}
              type="number"
              inputMode="numeric"
              min="1"
              max="100000"
              step="1"
              value={draft.budget_max}
              aria-invalid={budgetInvalid}
              aria-describedby={budgetInvalid ? `${id}-budget-error` : `${id}-budget-help`}
              onChange={(event) => updateDraft("budget_max", event.target.value)}
              disabled={saving}
              placeholder="未設定"
            /></span>
            <small id={`${id}-budget-help`}>留白代表不使用長期預算限制；當輪說明的預算仍優先。</small>
            {budgetInvalid ? <small id={`${id}-budget-error`} className={styles.fieldError}>請輸入 1–100,000 元。</small> : null}
          </label>

          <fieldset className={styles.textureField}>
            <legend>偏好質地</legend>
            <div className={styles.textureChoices}>
              {([
                [null, "未設定"],
                ["light", "清爽輕盈"],
                ["rich", "滋潤豐厚"],
              ] as const).map(([value, label]) => (
                <button
                  key={label}
                  type="button"
                  aria-pressed={draft.preferred_texture === value}
                  className={draft.preferred_texture === value ? styles.selected : ""}
                  onClick={() => updateDraft("preferred_texture", value)}
                  disabled={saving}
                >
                  {label}
                </button>
              ))}
            </div>
          </fieldset>

          <div className={styles.brandGrid}>
            {brandEditor("preferred_brands", "喜歡品牌", preferredBrandInput, setPreferredBrandInput)}
            {brandEditor("avoided_brands", "避用品牌", avoidedBrandInput, setAvoidedBrandInput)}
          </div>

          {error ? <p className={styles.error} role="alert">{error}</p> : null}
          {notice ? <p className={styles.notice} role="status" aria-live="polite">{notice}</p> : null}
          {!notice && dirty ? <p className={styles.dirty} role="status">有尚未儲存的推薦偏好。</p> : null}

          <div className={styles.actions}>
            {confirmReset ? (
              <div className={styles.resetConfirm} role="group" aria-label="確認撤回全部推薦偏好">
                <span>確定撤回全部設定？</span>
                <button type="button" className={styles.dangerButton} onClick={() => void handleReset()} disabled={saving}>確定撤回</button>
                <button type="button" className={styles.secondaryButton} onClick={() => setConfirmReset(false)} disabled={saving}>保留設定</button>
              </div>
            ) : (
              <button type="button" className={styles.secondaryButton} onClick={() => setConfirmReset(true)} disabled={saving}>全部撤回</button>
            )}
            <button
              type="button"
              className={styles.saveButton}
              onClick={() => void handleSave()}
              disabled={saving || budgetInvalid || !dirty}
            >
              {saving ? "處理中…" : "儲存推薦偏好"}
            </button>
          </div>
        </div>
      )}
    </section>
  );
}
