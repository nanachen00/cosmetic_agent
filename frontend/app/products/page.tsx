"use client";

import { useEffect, useMemo, useState } from "react";
import { Icon } from "@/components/Icon";
import { PageHeader } from "@/components/PageHeader";

const products = [
  { id: 1, brand: "MINT LAB", name: "清爽平衡保濕凝露", skin: "油性肌", effect: "保濕", price: 499, match: 94, notes: ["質地清爽", "無香料"] },
  { id: 2, brand: "LAVENDER DAY", name: "舒緩修護精華", skin: "敏感肌", effect: "舒緩", price: 680, match: 90, notes: ["屏障照護", "低刺激"] },
  { id: 3, brand: "COCOA SKIN", name: "溫和淨膚化妝水", skin: "油性肌", effect: "粉刺照護", price: 420, match: 86, notes: ["日常清潔", "清爽"] },
  { id: 4, brand: "MELLOW DEW", name: "水感防曬乳 SPF50", skin: "混合肌", effect: "防曬", price: 550, match: 82, notes: ["不黏膩", "易補擦"] }
];

function tokenizeSearch(value: string) {
  return value
    .toLowerCase()
    .split(/[\s,，、+＋/]+/)
    .map((term) => term.trim())
    .filter(Boolean);
}

export default function ProductsPage() {
  const [incoming, setIncoming] = useState("");
  const [draftQuery, setDraftQuery] = useState("");
  const [query, setQuery] = useState("");
  const [skin, setSkin] = useState("全部");
  const [selectedProduct, setSelectedProduct] = useState<(typeof products)[number] | null>(null);

  useEffect(() => {
    const value = new URLSearchParams(window.location.search).get("message") ?? "";
    setIncoming(value);
    if (value) {
      setDraftQuery(value);
      setQuery(value);
    }
  }, []);

  const results = useMemo(() => {
    const terms = tokenizeSearch(query);
    return products
      .map((item) => {
        const fields = [item.brand, item.name, item.skin, item.effect, ...item.notes]
          .map((field) => field.toLowerCase());
        const matchedTerms = terms.filter((term) => fields.some((field) => field.includes(term)));
        return {
          item,
          matchesSkin: skin === "全部" || item.skin === skin,
          matchedTerms: matchedTerms.length,
        };
      })
      .filter(({ matchesSkin, matchedTerms }) => matchesSkin && matchedTerms === terms.length)
      .sort((left, right) => right.matchedTerms - left.matchedTerms || right.item.match - left.item.match)
      .map(({ item }) => item);
  }, [query, skin]);

  return (
    <div className="content-wrap products-page">
      <PageHeader eyebrow="BEAUTY PICK" title="找產品" description="先收集需求，再交給商品搜尋與適配分析流程。"/>

      <section className="retro-frame product-search-panel">
        <form className="product-search-row" onSubmit={(event) => { event.preventDefault(); setQuery(draftQuery.trim()); }}>
          <Icon name="search"/>
          <input aria-label="產品需求" value={draftQuery} onChange={(event) => setDraftQuery(event.target.value)} placeholder="例如：油性肌、清爽保濕、無香料"/>
          <button type="submit" className="glossy-primary">搜尋</button>
        </form>
        <div className="filter-row"><span>膚質篩選</span>{["全部", "油性肌", "敏感肌", "混合肌"].map((item) => <button type="button" key={item} aria-pressed={skin === item} className={skin === item ? "active" : ""} onClick={() => setSkin(item)}>{item}</button>)}</div>
        {incoming ? <p className="search-context">已帶入首頁需求：「{incoming}」</p> : null}
      </section>

      <div className="result-heading"><div><span>AI MATCH RESULT</span><h2>推薦結果</h2></div><strong>{results.length} 項商品</strong></div>
      <section className="product-grid">
        {results.map((product) => (
          <article className="product-card" key={product.id}>
            <div className="match-badge">{product.match}%<span>MATCH</span></div>
            <div className="product-visual"><span>♡</span><small>{product.effect}</small></div>
            <p className="product-brand">{product.brand}</p>
            <h2>{product.name}</h2>
            <div className="product-tags">{product.notes.map((note) => <span key={note}>{note}</span>)}</div>
            <div className="product-bottom"><strong>NT${product.price}</strong><button type="button" aria-haspopup="dialog" onClick={() => setSelectedProduct(product)}>查看分析 <Icon name="arrow"/></button></div>
          </article>
        ))}
        {results.length === 0 ? <div className="empty-state"><Icon name="search"/><h2>暫時找不到完全符合的商品</h2><p>請改用「功效＋膚質」重新描述，例如：油性肌清爽保濕。</p></div> : null}
      </section>

      {selectedProduct ? (
        <div className="product-analysis-backdrop" role="presentation" onClick={() => setSelectedProduct(null)}>
          <section
            className="product-analysis-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="product-analysis-title"
            onClick={(event) => event.stopPropagation()}
          >
            <button
              type="button"
              className="product-analysis-close"
              aria-label="關閉產品分析"
              onClick={() => setSelectedProduct(null)}
            >×</button>
            <span className="product-analysis-eyebrow">PERSONAL FIT NOTE</span>
            <h2 id="product-analysis-title">{selectedProduct.name}</h2>
            <p className="product-analysis-score"><strong>{selectedProduct.match}% 適配</strong>・{selectedProduct.skin}</p>
            <dl className="product-analysis-list">
              <div><dt>主要訴求</dt><dd>{selectedProduct.effect}</dd></div>
              <div><dt>適合理由</dt><dd>{selectedProduct.notes.join("、")}</dd></div>
              <div><dt>參考價格</dt><dd>NT${selectedProduct.price}</dd></div>
            </dl>
            <p className="product-analysis-note">目前為展示商品資料；正式選購前仍建議確認完整成分表，並先做局部測試。</p>
          </section>
        </div>
      ) : null}
    </div>
  );
}
