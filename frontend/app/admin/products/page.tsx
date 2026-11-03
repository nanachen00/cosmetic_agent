'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { API_ORIGIN } from '../../../lib/api-origin';
import styles from './products.module.css';

type ReviewStatus = 'pending_review' | 'approved' | 'needs_enrichment' | 'excluded' | 'duplicate';
type Evidence = {
  source_id: string;
  source_name: string;
  source_url?: string | null;
  context?: string;
  rank?: number | null;
  time_bucket?: string;
  period_start?: string | null;
  period_end?: string | null;
};
type SafetyNote = {
  rule_id?: string;
  audiences?: string[];
  severity?: string;
  message: string;
  source_name?: string;
  source_url?: string;
};
type ProductCandidate = {
  canonical_key: string;
  brand: string;
  name_zh: string;
  name_en?: string;
  source_listing_name: string;
  official_name: string;
  taiwan_official_name: string;
  display_name_zh?: string;
  original_name?: string;
  common_names: string[];
  search_aliases: string[];
  name_verification_status: string;
  name_confidence: 'high' | 'medium' | 'low';
  name_sources: string[];
  ingredients_inci: string[];
  ingredients_source_text?: string;
  ingredients_source_language?: string;
  ingredient_normalization_status?: string;
  inci_status: 'full' | 'missing';
  inci_source_url?: string;
  formula_market?: string;
  ingredient_tags?: string[];
  unmapped_official_highlights?: string[];
  ingredient_evidence_level?: 'full_inci' | 'verified_tags' | 'unverified';
  ingredient_highlights_status?: 'verified' | 'missing';
  safety_notes?: SafetyNote[];
  recommendation_scope?: 'full_formula' | 'official_featured_ingredients_only' | 'not_verified';
  recommendation_limitations?: string;
  quality_blockers: string[];
  review_blockers: string[];
  canonical_family_key?: string;
  duplicate_candidates?: string[];
  capacity_variants?: string[];
  category: string;
  cohort: string;
  concerns: string[];
  functional_categories: string[];
  functional_labels: string[];
  suggested_functional_labels?: string[];
  canonical_brand: string;
  brand_market_key: string;
  brand_market_label: string;
  brand_origin_country: string;
  brand_origin_confidence: 'high' | 'medium' | 'low';
  manufacturer?: string;
  brand_owner?: string;
  tw_distributor?: string;
  brand_source_url?: string;
  evidence: Evidence[];
  review_status: ReviewStatus;
  reviewer_notes?: string;
  reviewed_at?: string | null;
  import_status?: 'not_requested' | 'failed' | 'imported';
  imported_at?: string | null;
  import_error?: string;
  neo4j_sku?: string | null;
  recommendable?: boolean;
};
type Stats = {
  total: number;
  status_counts: Record<string, number>;
  import_status_counts: Record<string, number>;
  functional_categories: Array<{ key: string; label: string; count: number }>;
  brand_markets: Array<{ key: string; label: string; count: number }>;
  brands: Array<{ brand: string; count: number }>;
  cohort_counts: Record<string, number>;
  notice: string;
  name_coverage: {
    verified_names: number;
    source_listing_names: number;
    with_common_names: number;
    with_inci: number;
    missing_inci: number;
    with_featured_ingredients: number;
    missing_featured_ingredients_all_records: number;
    featured_ingredient_eligible_total: number;
    with_featured_ingredients_eligible: number;
    missing_featured_ingredients_researchable: number;
    ingredient_identity_holds: number;
    not_in_active_catalog: number;
    with_safety_notes: number;
  };
};
type ProductPage = { items: ProductCandidate[]; total: number; page: number; limit: number };

const statusLabels: Record<ReviewStatus, string> = {
  pending_review: '待審核', approved: '已審核', needs_enrichment: '需補資料', excluded: '已排除', duplicate: '重複商品',
};
const cohortLabels: Record<string, string> = {
  classic: '跨年度經典', annual: '年度榜單', trending: '近期熱門', recent: '近月關注', new_release: '近期新品',
};
const blockerLabels: Record<string, string> = {
  missing_verified_taiwan_name: '缺台灣官方中文品名',
  missing_official_chinese_name: '缺可讀的中文品名',
  unverified_name_source: '品名來源尚未核對',
  not_a_canonical_product: '這是系列／促銷組，不是單一正式商品',
  unverified_brand: '品牌名稱或來源尚未核對',
  missing_full_inci: '完整 INCI 待補（不影響主打成分審核）',
  missing_verified_featured_ingredients: '缺商品專屬的官方主打成分與來源',
  missing_inci_source: '缺成分官方來源',
  formula_market_not_verified_tw: '尚未確認為台灣販售配方',
  duplicate_candidate: '已標記為重複商品',
  possible_size_variant_duplicate: '疑似只是容量不同，需合併為同一商品',
  suspected_hallucinated_name: '找不到對應官方商品，疑似錯誤或幻覺品名',
};

function productStatusLabel(item: ProductCandidate) {
  if (item.import_status === 'imported') return '已匯入正式商品庫';
  if (item.import_status === 'failed') return '匯入失敗，可重試';
  return statusLabels[item.review_status];
}

function displayProductTitle(item: ProductCandidate) {
  const displayName = item.taiwan_official_name || item.display_name_zh || item.original_name || item.official_name || item.name_zh;
  const alias = [...item.common_names, ...item.search_aliases].find((candidate) => {
    const normalized = candidate.trim().toLocaleLowerCase();
    return normalized && normalized !== displayName.trim().toLocaleLowerCase();
  });
  return alias ? `${displayName}（${alias}）` : displayName;
}

export default function ProductReviewPage() {
  const [data, setData] = useState<ProductPage>({ items: [], total: 0, page: 1, limit: 50 });
  const [stats, setStats] = useState<Stats | null>(null);
  const [status, setStatus] = useState('');
  const [functionKey, setFunctionKey] = useState('');
  const [cohort, setCohort] = useState('');
  const [brandMarket, setBrandMarket] = useState('');
  const [brand, setBrand] = useState('');
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState('');
  const [notes, setNotes] = useState<Record<string, string>>({});
  const locks = useRef(new Set<string>());

  const load = useCallback(async (page = 1) => {
    setLoading(true); setMessage('');
    try {
      const params = new URLSearchParams({ page: String(page), limit: '50' });
      if (status) params.set('review_status', status);
      if (functionKey) params.set('functional_category', functionKey);
      if (cohort) params.set('cohort', cohort);
      if (brandMarket) params.set('brand_market', brandMarket);
      if (brand) params.set('brand', brand);
      if (query) params.set('search', query);
      const [response, statsResponse] = await Promise.all([
        fetch(`${API_ORIGIN}/api/admin/product-reviews?${params}`, { credentials: 'include', cache: 'no-store' }),
        fetch(`${API_ORIGIN}/api/admin/product-reviews/stats`, { credentials: 'include', cache: 'no-store' }),
      ]);
      if (response.status === 401) throw new Error('請先登入 BLINK 帳號');
      if (response.status === 403) throw new Error('此帳號沒有 Admin 權限');
      if (!response.ok) throw new Error('目前無法讀取商品待審資料');
      const next = await response.json() as ProductPage;
      setData(next);
      setNotes((current) => ({ ...Object.fromEntries(next.items.map((item) => [item.canonical_key, item.reviewer_notes || ''])), ...current }));
      if (statsResponse.ok) setStats(await statsResponse.json() as Stats);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '載入失敗');
    } finally { setLoading(false); }
  }, [status, functionKey, cohort, brandMarket, brand, query]);

  useEffect(() => { void load(1); }, [load]);

  const pages = useMemo(() => Math.max(1, Math.ceil(data.total / data.limit)), [data]);

  async function update(item: ProductCandidate, reviewStatus: ReviewStatus) {
    if (locks.current.has(item.canonical_key)) return;
    locks.current.add(item.canonical_key); setMessage('');
    try {
      const response = await fetch(`${API_ORIGIN}/api/admin/product-reviews/${encodeURIComponent(item.canonical_key)}`, {
        method: 'PATCH', credentials: 'include', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ review_status: reviewStatus, reviewer_notes: notes[item.canonical_key] || '' }),
      });
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        throw new Error(body.detail || '審查狀態更新失敗');
      }
      await load(data.page);
      setMessage(reviewStatus === 'approved' ? '已通過並匯入 Neo4j 正式商品庫，現在可被推薦。' : '審查狀態已儲存。');
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : '更新失敗';
      await load(data.page);
      setMessage(errorMessage);
    }
    finally { locks.current.delete(item.canonical_key); }
  }

  function submitSearch(event: React.FormEvent) { event.preventDefault(); setQuery(search.trim()); }

  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <div><p className={styles.kicker}>BLINK ADMIN · PRODUCT REVIEW</p><h1>商品資料審查</h1><p>依保養功能、熱門期間與證據來源逐款確認；點擊通過後會自動建立向量並匯入正式商品庫。</p></div>
        <Link className={styles.back} href="/admin">返回營運後台</Link>
      </header>

      <section className={styles.overview}>
        <div><strong>{stats?.total ?? '—'}</strong><span>全部待審候選</span></div>
        <div><strong>{stats?.status_counts.pending_review ?? 0}</strong><span>尚未審核</span></div>
        <div><strong>{stats?.import_status_counts?.imported ?? 0}</strong><span>已匯入正式商品庫</span></div>
        <div><strong>{stats?.status_counts.needs_enrichment ?? 0}</strong><span>需要補成分／來源</span></div>
      </section>

      <section className={styles.safety}><strong>匯入規則</strong><span>{stats?.notice || '待審資料不會進入推薦；只有管理員點擊通過並成功匯入後才會上架。'}</span></section>

      <section className={styles.qualityGrid} aria-label="品名與成分完整度">
        <div><strong>{stats?.name_coverage?.verified_names ?? 0}</strong><span>官方品名已核對</span></div>
        <div><strong>{stats?.name_coverage?.with_common_names ?? 0}</strong><span>已收錄網路常用名</span></div>
        <div><strong>{stats?.name_coverage?.with_featured_ingredients_eligible ?? 0}</strong><span>有效商品已補官方主打成分</span></div>
        <div><strong>{stats?.name_coverage?.missing_featured_ingredients_researchable ?? 0}</strong><span>有效商品主打成分待補</span></div>
        <div><strong>{stats?.name_coverage?.ingredient_identity_holds ?? 0}</strong><span>版本／商品身分待釐清</span></div>
        <div><strong>{stats?.name_coverage?.not_in_active_catalog ?? 0}</strong><span>待審／重複／已排除（不算缺漏）</span></div>
        <div><strong>{stats?.name_coverage?.with_safety_notes ?? 0}</strong><span>已有族群注意事項</span></div>
        <div><strong>{stats?.name_coverage?.with_inci ?? 0}</strong><span>另有完整 INCI</span></div>
      </section>

      <section className={styles.functionGrid} aria-label="依功能分類">
        {(stats?.functional_categories || []).map((item) => (
          <button key={item.key} className={functionKey === item.key ? styles.functionActive : ''} onClick={() => setFunctionKey(functionKey === item.key ? '' : item.key)}>
            <strong>{item.count}</strong><span>{item.label}</span>
          </button>
        ))}
      </section>

      <section className={styles.marketGrid} aria-label="依品牌來源市場分類">
        {(stats?.brand_markets || []).map((item) => (
          <button key={item.key} className={brandMarket === item.key ? styles.marketActive : ''} onClick={() => setBrandMarket(brandMarket === item.key ? '' : item.key)}>
            <strong>{item.count}</strong><span>{item.label}</span>
          </button>
        ))}
      </section>

      <form className={styles.toolbar} onSubmit={submitSearch}>
        <label>審查狀態<select value={status} onChange={(event) => setStatus(event.target.value)}><option value="">全部</option>{Object.entries(statusLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        <label>時間類型<select value={cohort} onChange={(event) => setCohort(event.target.value)}><option value="">全部時段</option>{Object.entries(cohortLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label>
        <label>品牌<select value={brand} onChange={(event) => setBrand(event.target.value)}><option value="">全部品牌</option>{(stats?.brands || []).map((item) => <option key={item.brand} value={item.brand}>{item.brand}（{item.count}）</option>)}</select></label>
        <label className={styles.search}>搜尋商品<input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="中文品名、搜尋別名、品牌、英文名、俗稱或主打成分" /></label>
        <button type="submit">搜尋</button><span>{data.total} 款</span>
      </form>

      {message ? <p className={styles.message}>{message}</p> : null}
      {loading ? <p className={styles.empty}>載入中…</p> : null}
      {!loading && !data.items.length ? <p className={styles.empty}>目前沒有符合條件的候選商品。</p> : null}

      <section className={styles.list}>
        {data.items.map((item) => (
          <article className={styles.card} key={item.canonical_key}>
            <div className={styles.meta}><span>{productStatusLabel(item)}</span><span>{cohortLabels[item.cohort] || item.cohort}</span><span>{item.category}</span><span>{item.brand_market_label}</span></div>
            <p className={styles.brand}>{item.canonical_brand || item.brand}</p><h2>{displayProductTitle(item)}</h2>
            <p className={styles.nameStatus}>{['verified', 'source_verified_zh', 'official_tw', 'verified_tw'].includes(item.name_verification_status) ? '台灣正式品名已核對' : item.display_name_zh ? '中文譯名（待官方核對）' : '只有原文名稱，待補台灣正式品名'}</p>
            {item.official_name && item.official_name !== item.taiwan_official_name ? <p className={styles.english}><b>官方／原文名</b>{item.official_name}</p> : null}
            {item.original_name && item.original_name !== item.official_name ? <p className={styles.english}><b>英文／原文名</b>{item.original_name}</p> : null}
            <div className={styles.aliases}><strong>搜尋別名</strong>{item.search_aliases.length ? item.search_aliases.map((name) => <span key={name}>{name}</span>) : <em>暫無其他搜尋別名</em>}</div>
            <div className={styles.aliases}><strong>網路俗稱</strong>{item.common_names.length ? item.common_names.map((name) => <span key={name}>{name}</span>) : <em>尚未確認固定俗稱</em>}</div>
            <div className={styles.functionTags}>{item.functional_labels.map((label) => <b key={label}>{label}</b>)}</div>{item.suggested_functional_labels?.length ? <p className={styles.nameStatus}>文字推測（不是已驗證功效）：{item.suggested_functional_labels.join("、")}</p> : null}
            <dl><div><dt>功效狀態</dt><dd>{item.functional_labels.some((label) => label !== '綜合保養') ? '已有官方聲稱或配方證據' : '尚無足夠證據，不顯示為已驗證功效'}</dd></div><div><dt>品牌來源</dt><dd>{item.brand_origin_country}（{item.brand_origin_confidence === 'high' ? '高信心' : item.brand_origin_confidence === 'medium' ? '待補官方來源' : '需人工核對'}；非製造國）{item.brand_source_url ? <> · <a href={item.brand_source_url} target="_blank" rel="noreferrer">官方來源</a></> : null}</dd></div>{item.brand_owner ? <div><dt>品牌擁有者</dt><dd>{item.brand_owner}</dd></div> : null}{item.manufacturer ? <div><dt>製造商</dt><dd>{item.manufacturer}</dd></div> : null}{item.tw_distributor ? <div><dt>台灣公司／代理商</dt><dd>{item.tw_distributor}</dd></div> : null}{item.ingredient_tags?.length ? <div><dt>官方主打成分（已對應成分名）</dt><dd>{item.ingredient_tags.join('、')}</dd></div> : null}{item.unmapped_official_highlights?.length ? <div><dt>官方主打複合物／萃取名</dt><dd>{item.unmapped_official_highlights.join('、')}<br /><small>官方未公開精確 INCI，因此保留原稱，不自行猜成分。</small></dd></div> : null}{item.safety_notes?.length ? <div><dt>特定族群與使用注意</dt><dd><ul>{item.safety_notes.map((note) => <li key={note.rule_id || note.message}>{note.audiences?.length ? <strong>{note.audiences.join('、')}：</strong> : null}{note.message}{note.source_url ? <> · <a href={note.source_url} target="_blank" rel="noreferrer">依據</a></> : null}</li>)}</ul></dd></div> : null}<div><dt>資料可用範圍</dt><dd>{item.recommendation_limitations || '尚未取得足夠成分證據'}<br /><small>主打成分可用於功效解釋；完整 INCI 才能做較完整的過敏原與配方檢查。</small></dd></div><div><dt>官方全成分原文</dt><dd>{item.ingredients_source_text || '尚未取得官方完整成分表'}</dd></div><div><dt>正規化 Latin INCI</dt><dd>{item.ingredients_inci.length ? item.ingredients_inci.join('、') : '尚未完成；不會用主打成分或翻譯猜測代替'}</dd></div><div><dt>配方市場</dt><dd>{item.formula_market || '待核對'}</dd></div><div><dt>候選識別碼</dt><dd>{item.canonical_key}</dd></div></dl>
            <div className={styles.evidence}><strong>品質檢查</strong>{item.quality_blockers?.length ? item.quality_blockers.map((blocker) => <div key={blocker}><span>{blockerLabels[blocker] || blocker}</span></div>) : <div><span>已通過所有匯入條件</span></div>}</div><div className={styles.evidence}><strong>熱門／經典證據（不代表功效）</strong>{item.evidence.map((evidence, index) => <div key={`${evidence.source_id}-${index}`}><span>{evidence.context || '榜單入選'}</span><small>{evidence.source_name}{evidence.rank ? ` · 第 ${evidence.rank} 名` : ''}</small>{evidence.source_url ? <a href={evidence.source_url} target="_blank" rel="noreferrer">查看來源</a> : null}</div>)}</div>
            <label className={styles.notes}>審查備註<textarea rows={2} value={notes[item.canonical_key] || ''} onChange={(event) => setNotes((current) => ({ ...current, [item.canonical_key]: event.target.value }))} placeholder="例如：缺 INCI、品名需核對、同款不同容量…" /></label>
            {item.import_status === 'imported' ? (
              <div className={styles.actions}><button className={styles.approve} disabled>已匯入正式商品庫</button></div>
            ) : (
              <div className={styles.actions}><button className={styles.approve} disabled={(item.quality_blockers?.length || 0) > 0} title={item.quality_blockers?.length ? '請先補齊品質檢查項目' : undefined} onClick={() => void update(item, 'approved')}>{item.quality_blockers?.length ? '資料未完整，無法匯入' : item.import_status === 'failed' ? '重試匯入' : '通過並匯入'}</button><button onClick={() => void update(item, 'needs_enrichment')}>需補資料</button><button onClick={() => void update(item, 'duplicate')}>標記重複</button><button className={styles.exclude} onClick={() => void update(item, 'excluded')}>排除</button></div>
            )}
          </article>
        ))}
      </section>

      {pages > 1 ? <nav className={styles.pagination}><button disabled={data.page <= 1} onClick={() => void load(data.page - 1)}>上一頁</button><span>{data.page} / {pages}</span><button disabled={data.page >= pages} onClick={() => void load(data.page + 1)}>下一頁</button></nav> : null}
    </main>
  );
}
