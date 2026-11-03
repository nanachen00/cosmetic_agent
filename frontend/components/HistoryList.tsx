"use client";

import { HistoryCard } from "@/components/HistoryCard";
import type { SkinHistoryRecord } from "@/components/HistoryCard";
import type { SkinRecommendationsResponse } from "@/types/skin-tracking";

type HistoryListProps = {
  records: SkinHistoryRecord[];
};

type HistoryCareSummaryProps = {
  careTips: string[];
  recommendations: SkinRecommendationsResponse | null;
  recommendationsLoading: boolean;
};

function splitCareTip(tip: string): { label: string; detail: string } {
  const separator = tip.indexOf("：");
  if (separator <= 0 || separator > 12) return { label: "", detail: tip };
  return {
    label: tip.slice(0, separator),
    detail: tip.slice(separator + 1).trim()
  };
}

export function HistoryList({ records }: HistoryListProps) {
  if (records.length === 0) {
    return (
      <div className="history-empty-state">
        <IconPlaceholder />
        <div>
          <h3>這一天沒有膚況紀錄</h3>
          <p>請選擇上方有標記的日期。</p>
        </div>
      </div>
    );
  }

  return (
    <div className="history-list-panel">
      {records.map((record) => (
        <HistoryCard key={record.date} record={record} />
      ))}
    </div>
  );
}

/*
  保養方向屬於整個日期區段，而不是單一日期的右欄內容。
  獨立成元件後可由父層排成橫跨日曆與每日紀錄的完整寬度。
*/
export function HistoryCareSummary({
  careTips,
  recommendations,
  recommendationsLoading
}: HistoryCareSummaryProps) {
  return (
    <section className="history-care-summary">
        <div className="history-care-heading">
          <span
            className="history-care-icon"
            aria-hidden="true"
          >
            ✦
          </span>

          <div>
            <h3>這段期間的保養方向</h3>
            <p>依照所選日期的整體膚況紀錄整理</p>
          </div>
        </div>

        <div className="history-care-tips">
          {careTips.map((tip) => {
            const { label, detail } = splitCareTip(tip);
            return (
              <article className="history-care-tip-card" key={tip}>
                <div className="history-care-tip-copy">
                  {label ? <strong>{label}</strong> : <strong>照護重點</strong>}
                  <p>{detail}</p>
                </div>
              </article>
            );
          })}
        </div>

        <section className="history-product-recommendations">
          <div className="history-product-heading">
            <div>
              <h4>依近期紀錄篩選的商品候選</h4>
            </div>
            {recommendationsLoading ? (
              <p>正在比對膚況與商品資料…</p>
            ) : recommendations?.basis ? (
              <p>{recommendations.basis}</p>
            ) : null}
          </div>

          {recommendationsLoading ? (
            <div className="history-product-grid" aria-live="polite">
              {[0, 1, 2].map((item) => (
                <div className="history-product-card is-loading" key={item}>
                  正在整理商品候選…
                </div>
              ))}
            </div>
          ) : recommendations?.products.length ? (
            <div className="history-product-grid">
              {recommendations.products.map((product) => (
                <article className="history-product-card" key={product.id}>
                  <div className="history-product-meta">
                    <span>{product.brand || "品牌未標示"}</span>
                    <small>{product.category || "臉部保養"}</small>
                  </div>
                  <h5>{product.name}</h5>
                  <p>{product.reason}</p>
                  {product.concerns_zh?.length ? <p>對應膚況：{product.concerns_zh.join("、")}</p> : null}
                  {product.ingredients.length > 0 ? (
                    <div className="history-product-ingredients">
                      {product.ingredients.map((ingredient) => (
                        <span key={ingredient}>{ingredient}</span>
                      ))}
                    </div>
                  ) : null}
                  <div className="history-product-footer">
                    <strong>
                      {product.price !== null && product.price > 0
                        ? `NT$${product.price.toLocaleString()}`
                        : "價格請以通路為準"}
                    </strong>
                    {product.is_partner ? <span>合作商品</span> : <span>一般商品</span>}
                  </div>
                </article>
              ))}
            </div>
          ) : (
            <p className="history-product-empty">
              {recommendations?.message ?? "目前沒有適合顯示的商品候選。"}
            </p>
          )}

          {recommendations?.excluded_ingredients.length ? (
            <p className="history-product-exclusions">
              已依個人設定避開：{recommendations.excluded_ingredients.join("、")}
            </p>
          ) : null}
        </section>

        <small>
          商品為依紀錄篩選的保養候選，不代表治療效果；初次使用請查看完整標示並先做局部測試。
        </small>
    </section>
  );
}

/*
  空狀態使用簡單 CSS 圖形，
  不另外加入不符合目前網站風格的插畫。
*/
function IconPlaceholder() {
  return (
    <span
      className="history-empty-icon"
      aria-hidden="true"
    >
      ♡
    </span>
  );
}
