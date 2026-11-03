'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { API_ORIGIN } from '../../../lib/api-origin';
import styles from './content.module.css';

type ReviewStatus = 'pending_review' | 'in_review' | 'published' | 'rejected' | 'archived';
type DailyContent = {
  id: string;
  content_type: 'beauty_tip' | 'dermatologist_faq';
  title: string;
  summary: string;
  source_kind: string;
  source_name?: string | null;
  source_url?: string | null;
  review_status: ReviewStatus;
  reviewer_notes?: string | null;
  is_active: boolean;
  created_at: string;
  updated_at?: string;
  metadata?: {
    fetched_at?: string;
    card_locale?: string;
    localization_status?: string;
    requires_human_review?: boolean;
  } | null;
};

type ContentStats = {
  counts: Array<{ review_status: ReviewStatus; content_type: string; count: number }>;
  total: number;
  crawler_sources: number;
  latest_crawl_at: string | null;
  automation: { enabled: boolean; frequency: string; next_run_at?: string | null; last_result?: string | null };
};

const statusLabel: Record<ReviewStatus, string> = {
  pending_review: '待審核',
  in_review: '審核中',
  published: '已發布',
  rejected: '已退回',
  archived: '已封存',
};

export default function AdminContentPage() {
  const [items, setItems] = useState<DailyContent[]>([]);
  const [stats, setStats] = useState<ContentStats | null>(null);
  const [status, setStatus] = useState<string>('');
  const [contentType, setContentType] = useState('');
  const [keyword, setKeyword] = useState('');
  const [loading, setLoading] = useState(true);
  const [message, setMessage] = useState('');
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState('');
  const [draftSummary, setDraftSummary] = useState('');
  const [busyIds, setBusyIds] = useState<string[]>([]);
  const actionLocks = useRef(new Set<string>());

  const load = useCallback(async () => {
    setLoading(true);
    setMessage('');
    try {
      const params = new URLSearchParams({ limit: '100' });
      if (status) params.set('review_status', status);
      if (contentType) params.set('content_type', contentType);
      const [response, statsResponse] = await Promise.all([
        fetch(`${API_ORIGIN}/api/admin/daily-contents?${params.toString()}`, { credentials: 'include', cache: 'no-store' }),
        fetch(`${API_ORIGIN}/api/admin/daily-contents/stats`, { credentials: 'include', cache: 'no-store' }),
      ]);
      if (response.status === 401) throw new Error('請先登入 BLINK 帳號');
      if (response.status === 403) throw new Error('此帳號沒有 Admin 權限');
      if (!response.ok) throw new Error('目前無法讀取知識庫');
      const data = await response.json();
      setItems(data.items || []);
      if (statsResponse.ok) setStats(await statsResponse.json());
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '載入失敗');
    } finally {
      setLoading(false);
    }
  }, [status, contentType]);

  useEffect(() => { void load(); }, [load]);

  const update = async (item: DailyContent, review_status: ReviewStatus) => {
    // React state is asynchronous, so use a ref as the immediate re-entry guard.
    // This prevents a double click from creating two simultaneous PATCH requests.
    if (actionLocks.current.has(item.id)) return;
    actionLocks.current.add(item.id);
    setBusyIds((current) => current.includes(item.id) ? current : [...current, item.id]);
    setMessage('');
    try {
      const response = await fetch(`${API_ORIGIN}/api/admin/daily-contents/${item.id}`, {
        method: 'PATCH',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ review_status }),
      });
      if (!response.ok) {
        setMessage('更新失敗，請重新整理後再試');
        return;
      }
      // Hide the publish action immediately; load() then reconciles with the DB.
      setItems((current) => current.map((entry) => entry.id === item.id
        ? { ...entry, review_status, is_active: review_status === 'published' }
        : entry));
      setMessage(review_status === 'published' ? '已發布到每日知識卡片（重複點擊不會再次新增）' : '審核狀態已更新');
      await load();
    } finally {
      actionLocks.current.delete(item.id);
      setBusyIds((current) => current.filter((id) => id !== item.id));
    }
  };

  const saveText = async (item: DailyContent) => {
    const response = await fetch(`${API_ORIGIN}/api/admin/daily-contents/${item.id}`, {
      method: 'PATCH', credentials: 'include', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ title: draftTitle, summary: draftSummary }),
    });
    if (!response.ok) { setMessage('儲存失敗，請確認標題與摘要均有內容'); return; }
    setEditingId(null);
    setMessage('字卡繁中內容已儲存');
    await load();
  };

  const visibleItems = items.filter((item) => {
    const needle = keyword.trim().toLocaleLowerCase('zh-TW');
    if (!needle) return true;
    return `${item.title} ${item.summary} ${item.source_name || ''}`.toLocaleLowerCase('zh-TW').includes(needle);
  });

  const countFor = (reviewStatus: ReviewStatus) =>
    stats?.counts.filter((row) => row.review_status === reviewStatus).reduce((sum, row) => sum + Number(row.count), 0) || 0;

  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <div>
          <p className={styles.kicker}>BLINK ADMIN · CONTENT</p>
          <h1>知識內容審核</h1>
          <p>完整查看、校對與發布每日知識；英文官方原文保留為證據，字卡使用繁體中文。</p>
        </div>
        <Link className={styles.back} href="/admin">返回營運後台</Link>
      </header>

      <section className={styles.overview} aria-label="知識庫總覽">
        <div><strong>{stats?.total ?? '—'}</strong><span>全部知識</span></div>
        <div><strong>{countFor('pending_review')}</strong><span>待審核</span></div>
        <div><strong>{countFor('published')}</strong><span>已發布</span></div>
        <div><strong>{stats?.crawler_sources ?? '—'}</strong><span>官方來源</span></div>
      </section>

      <section className={styles.pipeline} aria-label="爬蟲狀態">
        <div><span className={stats?.automation.enabled ? styles.online : styles.manual} />
          <strong>{stats?.automation.enabled ? '自動爬取已啟用' : '目前為手動爬取'}</strong>
          <span>{stats?.automation.frequency || '尚未設定排程'}</span>
        </div>
        <div className={styles.scheduleTimes}>
          <span>最近爬取：{stats?.latest_crawl_at ? new Date(stats.latest_crawl_at).toLocaleString('zh-TW') : '尚無紀錄'}</span>
          <span>下次排程：{stats?.automation.next_run_at ? new Date(stats.automation.next_run_at).toLocaleString('zh-TW') : '尚未排定'}</span>
        </div>
      </section>

      <section className={styles.toolbar} aria-label="內容篩選">
        <label>
          審核狀態
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            <option value="">全部狀態</option>
            <option value="pending_review">待審核</option>
            <option value="in_review">審核中</option>
            <option value="published">已發布</option>
            <option value="rejected">已退回</option>
            <option value="archived">已封存</option>
          </select>
        </label>
        <label>
          內容類型
          <select value={contentType} onChange={(event) => setContentType(event.target.value)}>
            <option value="">全部類型</option>
            <option value="beauty_tip">美妝冷知識</option>
            <option value="dermatologist_faq">皮膚科醫師建議</option>
          </select>
        </label>
        <label className={styles.search}>
          搜尋完整知識庫
          <input value={keyword} onChange={(event) => setKeyword(event.target.value)} placeholder="輸入標題、內容或來源" />
        </label>
        <button type="button" onClick={() => void load()}>重新整理</button>
        <span className={styles.count}>{visibleItems.length} 筆</span>
      </section>

      {message ? <p className={styles.message}>{message}</p> : null}
      {loading ? <p className={styles.empty}>載入中…</p> : null}
      {!loading && items.length === 0 ? <p className={styles.empty}>目前沒有這個狀態的內容。</p> : null}

      <section className={styles.list} aria-live="polite">
        {visibleItems.map((item) => {
          const isBusy = busyIds.includes(item.id);
          return (
          <article className={styles.card} key={item.id} aria-busy={isBusy}>
            <div className={styles.meta}>
              <span>{item.content_type === 'beauty_tip' ? '美妝冷知識' : '皮膚科醫師建議'}</span>
              <span>{statusLabel[item.review_status]}</span>
              <span>{item.source_kind === 'crawler' ? 'Firecrawl 匯入' : item.source_kind}</span>
              <span>{item.metadata?.card_locale === 'zh-TW' ? '繁中卡片' : '待翻譯'}</span>
            </div>
            {editingId === item.id ? (
              <div className={styles.editor}>
                <label>字卡標題<input value={draftTitle} onChange={(event) => setDraftTitle(event.target.value)} /></label>
                <label>字卡摘要<textarea rows={4} value={draftSummary} onChange={(event) => setDraftSummary(event.target.value)} /></label>
                <div className={styles.actions}>
                  <button className={styles.publish} type="button" onClick={() => void saveText(item)}>儲存文字</button>
                  <button type="button" onClick={() => setEditingId(null)}>取消</button>
                </div>
              </div>
            ) : (
              <><h2>{item.title}</h2><p className={styles.summary}>{item.summary}</p></>
            )}
            <div className={styles.source}>
              <strong>來源：</strong>{item.source_name || '未標示'}
              {item.source_url ? <a href={item.source_url} target="_blank" rel="noreferrer">查看原文</a> : null}
            </div>
            <div className={styles.actions}>
              {editingId !== item.id ? (
                <button type="button" disabled={isBusy} onClick={() => { setEditingId(item.id); setDraftTitle(item.title); setDraftSummary(item.summary); }}>編輯繁中卡片</button>
              ) : null}
              {item.review_status !== 'published' ? (
                <button className={styles.publish} type="button" disabled={isBusy} onClick={() => void update(item, 'published')}>
                  {isBusy ? '發布中…' : '確認並發布'}
                </button>
              ) : null}
              {item.review_status === 'pending_review' ? (
                <button type="button" disabled={isBusy} onClick={() => void update(item, 'in_review')}>標記審核中</button>
              ) : null}
              {item.review_status !== 'rejected' ? (
                <button type="button" disabled={isBusy} onClick={() => void update(item, 'rejected')}>退回</button>
              ) : null}
              {item.review_status === 'published' ? (
                <button type="button" disabled={isBusy} onClick={() => void update(item, 'archived')}>下架並封存</button>
              ) : null}
            </div>
          </article>
          );
        })}
      </section>
    </main>
  );
}
