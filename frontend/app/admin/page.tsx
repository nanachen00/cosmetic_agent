'use client';

import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react';
import { API_ORIGIN } from '../../lib/api-origin';
import styles from './admin.module.css';

type DistributionItem = { key: string; count: number; records?: number };

type GpuDevice = {
  id: string;
  utilization_percent: number | null;
  vram_used_mb: number | null;
  vram_total_mb: number | null;
  vram_percent: number | null;
  temperature_c: number | null;
  memory_temperature_c: number | null;
  power_w: number | null;
  throttle_status: string;
  ecc_correctable: number;
  ecc_uncorrectable: number;
};

type Overview = {
  status: string;
  service: string;
  environment: string;
  release: string;
  active_chat_sessions: number;
  platform: {
    available: boolean;
    definitions: Record<string, string>;
    accounts: {
      total: number;
      login_enabled: number;
      pending_activation: number;
      suspended: number;
      new_last_7_days: number;
      new_previous_7_days: number;
      signup_growth_percent: number;
    };
    engagement: {
      active_30d: number;
      inactive_30d: number;
      active_7d: number;
      returning_30d: number;
      ai_users_30d: number;
      checkin_users_7d: number;
      active_rate_30d: number;
      returning_rate_30d: number;
      ai_adoption_rate_30d: number;
      checkin_rate_7d: number;
    };
    signups: Array<{ day: string; count: number }>;
    checkin_streaks: DistributionItem[];
    personality_distribution: DistributionItem[];
    concern_distribution: DistributionItem[];
    customer_segments: DistributionItem[];
  };
  gpu: GpuSnapshot;
  telemetry: {
    errors_configured: boolean;
    logs_configured: boolean;
    tracing_enabled: boolean;
    session_replay_enabled: boolean;
  };
  links: { errors: string | null; logs: string | null; metrics: string | null };
};

type AdminUser = {
  id: string;
  username: string | null;
  display_name: string | null;
  email: string | null;
  status: string;
  providers: string[];
  created_at: string | null;
  last_login_at: string | null;
  resources: {
    conversations: number;
    quiz_attempts: number;
    daily_checkins: number;
    skin_records: number;
    uploaded_assets: number;
    live_sessions: number;
  };
};

type GpuProcess = {
  pid: number | null;
  name: string;
  vram_mb: number | null;
  namespace_limited: boolean;
};


type GpuHistoryPoint = {
  timestamp: string;
  device_id: string;
  device_name: string;
  utilization_percent: number | null;
  vram_percent: number | null;
  vram_used_mb: number | null;
  vram_total_mb: number | null;
  temperature_c: number | null;
  power_w: number | null;
  processes: GpuProcess[];
};


type GpuHistoryProcess = GpuProcess & {
  first_seen: string;
  last_seen: string;
  latest_vram_mb: number;
  max_vram_mb: number;
};


type GpuHistory = {
  window: string;
  sample_interval_seconds: number;
  retention_days: number;
  persistence_available: boolean;
  points: GpuHistoryPoint[];
  processes: GpuHistoryProcess[];
};

type GpuSnapshot = {
  available: boolean;
  source: string;
  collected_at?: string;
  devices: GpuDevice[];
  processes: GpuProcess[];
  process_names_available: boolean;
  history?: GpuHistory;
};

type DataHealth = {
  collected_at: string;
  postgres: {
    available: boolean;
    summary?: Record<string, string | number | null>;
    tables: Array<{ name: string; estimated_rows: number; size_bytes: number }>;
  };
  neo4j: {
    available: boolean;
    total_nodes?: number;
    total_relations?: number;
    nodes: Array<{ label: string; count: number }>;
    relations: Array<{ relation: string; count: number }>;
  };
};

type KnowledgeRecord = {
  id: string;
  display_name?: string;
  data?: Record<string, unknown>;
  relation?: string;
  source_name?: string;
  target_name?: string;
  source_labels?: string[];
  target_labels?: string[];
};

type KnowledgePage = {
  entity: string;
  items: KnowledgeRecord[];
  total: number;
  page: number;
  limit: number;
};

type UserDetail = {
  profile: Record<string, unknown>;
  recent: Record<string, Array<Record<string, unknown>>>;
};

type UserPage = { items: AdminUser[]; total: number; page: number; limit: number };

const number = new Intl.NumberFormat('zh-TW');

const PERSONALITY_LABELS: Record<string, string> = {
  explorer: '探索型', review_dependent: '評價依賴型', function_oriented: '功效導向型',
  brand_oriented: '品牌導向型', price_sensitive: '價格敏感型', impulse_buyer: '衝動購買型',
  unclassified: '未分類',
};
const STREAK_LABELS: Record<string, string> = { none: '尚未形成', days_1_2: '連續 1–2 天', days_3_6: '連續 3–6 天', days_7_13: '連續 7–13 天', days_14_plus: '連續 14 天以上' };
const SEGMENT_LABELS: Record<string, string> = { habit: '養成型會員', growing: '成長型會員', first_touch: '初次體驗', unactivated: '尚未啟動', dormant: '待喚回' };
const CONCERN_LABELS: Record<string, string> = { dryness: '乾燥', acne: '痘痘', redness: '泛紅', oiliness: '出油', dark_spot: '暗沉斑點', itching: '搔癢', other: '其他' };
const PIE_COLORS = ['#4b8b77', '#8c6b96', '#d49870', '#6e8fb5', '#c7a34b', '#a66c6c', '#7f9c73'];

function labelFor(key: string, labels: Record<string, string>) {
  return labels[key] || key.replaceAll('_', ' ');
}

function distributionTotal(items: DistributionItem[]) {
  return items.reduce((sum, item) => sum + item.count, 0);
}

function pieGradient(items: DistributionItem[]) {
  const total = distributionTotal(items);
  if (!total) return '#edf0eb';
  let cursor = 0;
  const slices = items.map((item, index) => {
    const start = cursor;
    cursor += (item.count / total) * 100;
    return `${PIE_COLORS[index % PIE_COLORS.length]} ${start.toFixed(2)}% ${cursor.toFixed(2)}%`;
  });
  return `conic-gradient(${slices.join(", ")} )`;
}



function formatDate(value: string | null) {
  if (!value) return '尚未登入';
  return new Date(value).toLocaleString('zh-TW', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function valueOrDash(value: number | null, suffix = '') {
  return value === null ? '—' : Math.round(value) + suffix;
}

function formatBytes(value: number | null | undefined) {
  if (value === null || value === undefined) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let current = value;
  let unit = 0;
  while (current >= 1024 && unit < units.length - 1) {
    current /= 1024;
    unit += 1;
  }
  return current.toFixed(unit > 1 ? 1 : 0) + ' ' + units[unit];
}

function displayValue(value: unknown) {
  if (value === null || value === undefined || value === '') return '—';
  if (Array.isArray(value)) return value.map((item) => String(item)).join('、') || '—';
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

function recentGroupLabel(group: string) {
  const labels: Record<string, string> = { conversations: '近期對話', skin_records: '近期肌膚紀錄', quiz_attempts: '近期測驗', daily_checkins: '近期簽到' };
  return labels[group] || group;
}

function MetricBar({ label, value, suffix, tone = 'mint' }: { label: string; value: number | null; suffix: string; tone?: 'mint' | 'lavender' | 'amber' }) {
  const width = Math.min(100, Math.max(0, value || 0));
  return (
    <div className={styles.metricBar}>
      <div className={styles.metricLabel}><span>{label}</span><strong>{valueOrDash(value, suffix)}</strong></div>
      <div className={styles.track}><span className={styles[tone]} style={{ width: width + '%' }} /></div>
    </div>
  );
}

function formatGpuTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function formatGpuDateTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return date.toLocaleString('zh-TW', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function GpuTimelineChart({
  points,
  metric,
  title,
  color,
}: {
  points: GpuHistoryPoint[];
  metric: 'utilization_percent' | 'vram_percent';
  title: string;
  color: string;
}) {
  const width = Math.max(760, points.length * 16);
  const height = 230;
  const top = 18;
  const bottom = 42;
  const left = 46;
  const right = 18;
  const chartHeight = height - top - bottom;
  const chartWidth = width - left - right;
  const plotted = points.map((point, index) => {
    const value = Math.max(0, Math.min(100, Number(point[metric] ?? 0)));
    const x = left + (points.length <= 1 ? 0 : (index / (points.length - 1)) * chartWidth);
    const y = top + chartHeight - (value / 100) * chartHeight;
    return { point, value, x, y };
  });
  const path = plotted.map((item, index) => (index === 0 ? 'M' : 'L') + ' ' + item.x.toFixed(1) + ' ' + item.y.toFixed(1)).join(' ');
  const labelStep = Math.max(1, Math.ceil(points.length / 6));

  return (
    <article className={styles.timelineCard}>
      <div className={styles.timelineHeader}>
        <div><span className={styles.kicker}>TIMELINE</span><h3>{title}</h3></div>
        <small>{points.length ? points.length + ' 筆記錄' : '等待第一筆記錄'}</small>
      </div>
      <div className={styles.timelineScroller} tabIndex={0} aria-label={title + '，可水平捲動時間軸'}>
        {points.length ? (
          <svg className={styles.timelineSvg} viewBox={'0 0 ' + width + ' ' + height} style={{ width }} role="img" aria-label={title + '折線圖'}>
            {[0, 25, 50, 75, 100].map((tick) => {
              const y = top + chartHeight - (tick / 100) * chartHeight;
              return <g key={tick}><line x1={left} x2={width - right} y1={y} y2={y} className={styles.timelineGridLine} /><text x={8} y={y + 4} className={styles.timelineAxisText}>{tick}%</text></g>;
            })}
            <path d={path} fill="none" stroke={color} strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
            {plotted.map(({ point, value, x, y }, index) => {
              const topProcess = [...(point.processes || [])].sort((a, b) => Number(b.vram_mb || 0) - Number(a.vram_mb || 0))[0];
              const showLabel = index % labelStep === 0 || index === plotted.length - 1;
              const tooltip = formatGpuDateTime(point.timestamp) + '｜' + value.toFixed(1) + '%' + (topProcess ? '｜' + topProcess.name + ' (PID ' + (topProcess.pid ?? '—') + ')' : '｜無 GPU 程序');
              return (
                <g key={point.timestamp + '-' + index}>
                  <circle cx={x} cy={y} r="3.5" fill={color}><title>{tooltip}</title></circle>
                  {showLabel ? <text x={x} y={height - 14} textAnchor="middle" className={styles.timelineAxisText}>{formatGpuTime(point.timestamp)}</text> : null}
                </g>
              );
            })}
          </svg>
        ) : <div className={styles.timelineEmpty}>正在建立歷史資料，約 5 秒後會出現第一個節點。</div>}
      </div>
      <p className={styles.timelineHint}>時間軸會持續記錄；可用滑鼠滾輪、觸控板或鍵盤左右移動。</p>
    </article>
  );
}



export default function AdminPage() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [users, setUsers] = useState<UserPage>({ items: [], total: 0, page: 1, limit: 25 });
  const [loading, setLoading] = useState(true);
  const [usersLoading, setUsersLoading] = useState(true);
  const [message, setMessage] = useState('');
  const [search, setSearch] = useState('');
  const [updatedAt, setUpdatedAt] = useState('');
  const [gpuSnapshot, setGpuSnapshot] = useState<GpuSnapshot | null>(null);
  const [gpuWindow, setGpuWindow] = useState('1h');
  const [dataHealth, setDataHealth] = useState<DataHealth | null>(null);
  const [knowledge, setKnowledge] = useState<KnowledgePage | null>(null);
  const [knowledgeEntity, setKnowledgeEntity] = useState('products');
  const [knowledgeSearch, setKnowledgeSearch] = useState('');
  const [selectedUser, setSelectedUser] = useState<AdminUser | null>(null);
  const [userDetail, setUserDetail] = useState<UserDetail | null>(null);
  const [dataLoading, setDataLoading] = useState(true);
  const [knowledgeLoading, setKnowledgeLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);

  const loadOverview = useCallback(async (showLoading = true) => {
    if (showLoading) setLoading(true);
    setMessage('');
    try {
      const response = await fetch(API_ORIGIN + '/api/admin/overview', { credentials: 'include', cache: 'no-store' });
      if (response.status === 401) throw new Error('請先使用 BLINK 帳號登入。');
      if (response.status === 403) throw new Error('此帳號沒有管理員權限。');
      if (!response.ok) throw new Error('目前無法讀取營運狀態，請稍後再試。');
      const data = (await response.json()) as Overview;
      setOverview(data);
      setUpdatedAt(new Date().toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '載入失敗');
    } finally {
      setLoading(false);
    }
  }, []);

  const loadUsers = useCallback(async (query = '', page = 1) => {
    setUsersLoading(true);
    try {
      const params = new URLSearchParams({ search: query, page: String(page), limit: '25' });
      const response = await fetch(API_ORIGIN + '/api/admin/users?' + params.toString(), { credentials: 'include', cache: 'no-store' });
      if (!response.ok) throw new Error('無法讀取會員資料');
      setUsers((await response.json()) as UserPage);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '會員資料載入失敗');
    } finally {
      setUsersLoading(false);
    }
  }, []);

    const loadGpu = useCallback(async (windowKey: string) => {
    try {
      const response = await fetch(API_ORIGIN + '/api/admin/gpu?window=' + encodeURIComponent(windowKey), { credentials: 'include', cache: 'no-store' });
      if (!response.ok) return;
      const gpu = (await response.json()) as GpuSnapshot;
      setGpuSnapshot(gpu);
      setOverview((current) => current ? { ...current, gpu } : current);
      setUpdatedAt(new Date().toLocaleTimeString('zh-TW', { hour: '2-digit', minute: '2-digit', second: '2-digit' }));
    } catch {
      // 保留最後一筆有效資料，避免短暫斷線讓圖表清空。
    }
  }, []);

  const loadDataHealth = useCallback(async () => {
    setDataLoading(true);
    try {
      const response = await fetch(API_ORIGIN + '/api/admin/data-health', { credentials: 'include', cache: 'no-store' });
      if (!response.ok) throw new Error('無法讀取資料庫健康狀態');
      setDataHealth((await response.json()) as DataHealth);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '資料庫健康狀態載入失敗');
    } finally {
      setDataLoading(false);
    }
  }, []);

  const loadKnowledge = useCallback(async (entity = 'products', query = '', page = 1) => {
    setKnowledgeLoading(true);
    try {
      const params = new URLSearchParams({ entity, search: query, page: String(page), limit: '20' });
      const response = await fetch(API_ORIGIN + '/api/admin/knowledge?' + params.toString(), { credentials: 'include', cache: 'no-store' });
      if (!response.ok) throw new Error('無法讀取知識圖譜');
      setKnowledge((await response.json()) as KnowledgePage);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '知識圖譜載入失敗');
    } finally {
      setKnowledgeLoading(false);
    }
  }, []);

  const openUserDetail = useCallback(async (user: AdminUser) => {
    setSelectedUser(user);
    setUserDetail(null);
    setDetailLoading(true);
    try {
      const response = await fetch(API_ORIGIN + '/api/admin/users/' + user.id, { credentials: 'include', cache: 'no-store' });
      if (!response.ok) throw new Error('無法讀取會員詳情');
      setUserDetail((await response.json()) as UserDetail);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '會員詳情載入失敗');
    } finally {
      setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    void Promise.all([loadOverview(), loadUsers(), loadDataHealth(), loadKnowledge()]);
  }, [loadOverview, loadUsers, loadDataHealth, loadKnowledge]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void loadOverview(false);
    }, 30000);
    return () => window.clearInterval(timer);
  }, [loadOverview]);

    useEffect(() => {
    void loadGpu(gpuWindow);
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void loadGpu(gpuWindow);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [loadGpu, gpuWindow]);

const maxSignup = useMemo(() => Math.max(1, ...(overview?.platform?.signups || []).map((item) => item.count)), [overview]);

  async function changeStatus(user: AdminUser) {
    const nextStatus = user.status === 'suspended' ? 'active' : 'suspended';
    const action = nextStatus === 'suspended' ? '停用' : '恢復';
    if (!window.confirm('確定要' + action + '「' + (user.username || user.display_name || user.email || '此會員') + '」嗎？')) return;
    const response = await fetch(API_ORIGIN + '/api/admin/users/' + user.id + '/status', {
      method: 'PATCH',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: nextStatus }),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      window.alert(body.detail || '狀態更新失敗');
      return;
    }
    await Promise.all([loadUsers(search, users.page), loadOverview(false)]);
  }

  function submitSearch(event: FormEvent) {
    event.preventDefault();
    void loadUsers(search, 1);
  }

  if (message && !overview) {
    return (
      <main className={styles.centerPage}>
        <div className={styles.accessCard}>
          <span className={styles.logoMark}>B</span>
          <p className={styles.eyebrow}>BLINK AI · ADMIN</p>
          <h1>管理中心</h1>
          <p>{message}</p>
          <a href="/login?next=/admin" className={styles.primaryButton}>前往登入</a>
        </div>
      </main>
    );
  }

  const platform = overview?.platform;
  const accounts = platform?.accounts;
  const engagement = platform?.engagement;
  const personalities = platform?.personality_distribution || [];
  const streaks = platform?.checkin_streaks || [];
  const segments = platform?.customer_segments || [];
  const concerns = platform?.concern_distribution || [];
  const topConcern = concerns[0];
  const topPersonality = personalities[0];
  const maxStreak = Math.max(1, ...streaks.map((item) => item.count));
  const maxSegment = Math.max(1, ...segments.map((item) => item.count));
  const maxConcern = Math.max(1, ...concerns.map((item) => item.count));

  return (
    <main className={styles.page}>
      <aside className={styles.sidebar}>
        <a className={styles.brand} href="/" aria-label="回到 BLINK AI 首頁">
          <span className={styles.logoMark}>B</span>
          <span><b>BLINK AI</b><small>管理中心</small></span>
        </a>
        <nav>
          <a className={styles.navActive} href="#overview"><span>⌁</span>營運總覽</a>
          <a href="#gpu"><span>◫</span>GPU 監控</a>
          <a href="#users"><span>♙</span>會員管理</a>
          <a href="#data-health"><span>▦</span>資料庫健康</a>
          <a href="#knowledge"><span>◇</span>知識圖譜</a>
          <a href="/admin/products"><span>▣</span>商品審查庫</a>
          <a href="#observability"><span>◎</span>監控中心</a>
        </nav>
        <div className={styles.sidebarFoot}>
          <span className={styles.statusDot} />
          <div><b>正式環境</b><small>{overview?.release || '讀取中'}</small></div>
        </div>
      </aside>

      <div className={styles.content}>
        <header className={styles.topbar}>
          <div>
            <p className={styles.eyebrow}>BLINK AI · OPERATIONS</p>
            <h1>早安，管理員</h1>
            <p>用成長、留存與客群分布判斷下一步營運行動。</p>
          </div>
          <div className={styles.topActions}>
            <span className={styles.livePill}><i />GPU 每 5 秒更新</span>
            <button type="button" onClick={() => void Promise.all([loadOverview(), loadUsers(search, users.page), loadDataHealth(), loadKnowledge(knowledgeEntity, knowledgeSearch, knowledge?.page || 1)])} disabled={loading}>↻ 重新整理</button>
          </div>
        </header>

        {message ? <div className={styles.notice}>{message}</div> : null}

        <section id="overview" className={styles.section}>
          <div className={styles.sectionHeading}>
            <div><span className={styles.kicker}>BUSINESS OVERVIEW</span><h2>成長、活躍與客群洞察</h2></div>
            <small>最後更新 {updatedAt || '—'}</small>
          </div>
          <div className={styles.statGrid}>
            <article className={styles.statCard}><span className={styles.statIcon}>♙</span><div><small>註冊帳號總數</small><strong>{accounts ? number.format(accounts.total) : '—'}</strong><p><b>+{accounts?.new_last_7_days || 0}</b> 近 7 天新註冊</p></div></article>
            <article className={styles.statCard}><span className={styles.statIcon}>✓</span><div><small>可登入帳號</small><strong>{accounts ? number.format(accounts.login_enabled) : '—'}</strong><p>帳號狀態為 active；另有 {accounts?.pending_activation || 0} 位待啟用、{accounts?.suspended || 0} 位停用</p></div></article>
            <article className={styles.statCard}><span className={styles.statIcon}>↗</span><div><small>近 30 日有互動會員</small><strong>{engagement ? number.format(engagement.active_30d) : '—'}</strong><p><b>{engagement?.active_rate_30d?.toFixed(1) || '0.0'}%</b> 曾使用 AI、簽到、測驗或肌膚追蹤</p></div></article>
            <article className={styles.statCard}><span className={styles.statIcon}>⌁</span><div><small>近 7 日簽到會員率</small><strong>{engagement ? engagement.checkin_rate_7d.toFixed(1) + '%' : '—'}</strong><p>{engagement?.checkin_users_7d || 0} / {accounts?.login_enabled || 0} 位可登入會員有簽到</p></div></article>
          </div>

          <div className={styles.actionGrid}>
            <article className={styles.insightCard}><small>留存訊號</small><strong>{engagement?.returning_rate_30d?.toFixed(1) || '0.0'}%</strong><h3>30 日回訪會員率</h3><p>{engagement?.returning_30d || 0} 位會員在至少兩個不同日期使用平台；可針對僅使用一次的會員設計第二次回訪誘因。</p></article>
            <article className={styles.insightCard}><small>AI 採用</small><strong>{engagement?.ai_adoption_rate_30d?.toFixed(1) || '0.0'}%</strong><h3>近 30 日 AI 使用率</h3><p>{engagement?.ai_users_30d || 0} 位可登入會員使用過 AI；這裡看採用人數，不再用沒有決策價值的對話總次數。</p></article>
            <article className={styles.insightCard}><small>喚回機會</small><strong>{engagement?.inactive_30d || 0}</strong><h3>近 30 日尚未互動</h3><p>這些帳號可優先安排導覽、內容推薦或提醒，不把它們模糊地稱為「無效會員」。</p></article>
            <article className={styles.insightCard}><small>主要肌膚需求</small><strong>{topConcern ? labelFor(topConcern.key, CONCERN_LABELS) : '—'}</strong><h3>{topConcern ? topConcern.count + ' 位會員提及' : '尚無資料'}</h3><p>可優先規劃相關知識內容、產品推薦與活動主題。</p></article>
          </div>

          <div className={styles.analyticsGrid}>
            <article className={styles.panel}>
              <div className={styles.panelTitle}><div><span className={styles.kicker}>ACQUISITION</span><h3>近 7 天會員成長</h3></div><div className={styles.metricStack}><strong>+{accounts?.new_last_7_days || 0}</strong><small>較前 7 天 {accounts?.signup_growth_percent && accounts.signup_growth_percent > 0 ? '+' : ''}{accounts?.signup_growth_percent?.toFixed(1) || '0.0'}%</small></div></div>
              <div className={styles.barChart}>
                {(platform?.signups || []).map((item) => (
                  <div key={item.day} className={styles.barColumn} title={item.day + '：' + item.count + ' 位'}>
                    <span>{item.count || ''}</span><i style={{ height: Math.max(8, (item.count / maxSignup) * 100) + '%' }} /><small>{new Date(item.day).toLocaleDateString('zh-TW', { weekday: 'short' })}</small>
                  </div>
                ))}
              </div>
            </article>

            <article className={styles.panel}>
              <div className={styles.panelTitle}><div><span className={styles.kicker}>CHECK-IN HABIT</span><h3>目前連續簽到分布</h3></div><small>以可登入帳號為母體</small></div>
              <div className={styles.distributionBars}>
                {streaks.map((item) => <div key={item.key}><div><span>{labelFor(item.key, STREAK_LABELS)}</span><b>{item.count} 位</b></div><i><span style={{ width: (item.count / maxStreak) * 100 + '%' }} /></i></div>)}
              </div>
              <p className={styles.chartNote}>目前連續天數從最近簽到日回推；超過一天未簽到即歸為「尚未形成」。</p>
            </article>

            <article className={styles.panel}>
              <div className={styles.panelTitle}><div><span className={styles.kicker}>PERSONALITY</span><h3>人格測驗客群分布</h3></div><small>{distributionTotal(personalities)} 份完成結果</small></div>
              <div className={styles.pieLayout}>
                <div className={styles.pieChart} style={{ background: pieGradient(personalities) }}><span>{distributionTotal(personalities)}<small>已完成</small></span></div>
                <div className={styles.chartLegend}>
                  {personalities.map((item, index) => <div key={item.key}><i style={{ background: PIE_COLORS[index % PIE_COLORS.length] }} /><span>{labelFor(item.key, PERSONALITY_LABELS)}</span><b>{item.count} · {distributionTotal(personalities) ? ((item.count / distributionTotal(personalities)) * 100).toFixed(0) : 0}%</b></div>)}
                </div>
              </div>
              <p className={styles.chartNote}>{topPersonality ? labelFor(topPersonality.key, PERSONALITY_LABELS) + '是目前最大客群，可用來調整推薦文案與內容排序。' : '完成測驗後會在這裡形成客群輪廓。'}</p>
            </article>

            <article className={styles.panel}>
              <div className={styles.panelTitle}><div><span className={styles.kicker}>ENGAGEMENT SEGMENTS</span><h3>會員互動階段</h3></div><small>近 30 日行為</small></div>
              <div className={styles.distributionBars}>
                {segments.map((item) => <div key={item.key}><div><span>{labelFor(item.key, SEGMENT_LABELS)}</span><b>{item.count} 位</b></div><i><span style={{ width: (item.count / maxSegment) * 100 + '%' }} /></i></div>)}
              </div>
              <div className={styles.segmentGuide}><span><b>養成型</b> 7 天以上有互動</span><span><b>成長型</b> 2–6 天有互動</span><span><b>初次體驗</b> 僅 1 天</span><span><b>尚未啟動／待喚回</b> 30 日零互動</span></div>
            </article>

            <article className={styles.panelWide}>
              <div className={styles.panelTitle}><div><span className={styles.kicker}>CUSTOMER NEEDS</span><h3>會員主要肌膚需求</h3></div><small>依提及會員數，非紀錄總筆數</small></div>
              <div className={styles.concernGrid}>
                {concerns.slice(0, 6).map((item) => <div key={item.key}><div><span>{labelFor(item.key, CONCERN_LABELS)}</span><b>{item.count} 位</b></div><i><span style={{ width: (item.count / maxConcern) * 100 + '%' }} /></i><small>{item.records || item.count} 筆紀錄</small></div>)}
              </div>
            </article>
          </div>
        </section>

                <section id="gpu" className={styles.section}>
          <div className={styles.sectionHeading}>
            <div><span className={styles.kicker}>HARDWARE</span><h2>AMD GPU 時間軸監控</h2></div>
            <span className={(gpuSnapshot ?? overview?.gpu)?.available ? styles.connected : styles.disconnected}>{(gpuSnapshot ?? overview?.gpu)?.available ? 'amd-smi 已連線' : '無法讀取 GPU'}</span>
          </div>

          <div className={styles.gpuControlBar}>
            <div className={styles.rangeTabs} aria-label="GPU 歷史時段">
              {(['15m', '1h', '6h', '24h'] as const).map((range) => (
                <button key={range} type="button" className={gpuWindow === range ? styles.rangeActive : styles.rangeButton} onClick={() => setGpuWindow(range)}>
                  {({ '15m': '15 分鐘', '1h': '1 小時', '6h': '6 小時', '24h': '24 小時' } as Record<string, string>)[range]}
                </button>
              ))}
            </div>
            <div className={styles.gpuRecordStatus}>
              <span className={styles.liveDot} />每 5 秒更新
              <small>{gpuSnapshot?.history?.persistence_available ? '伺服器保存 7 日' : '等待伺服器歷史資料'}</small>
            </div>
          </div>

          <div className={styles.gpuGrid}>
            {((gpuSnapshot ?? overview?.gpu)?.devices || []).map((gpu) => {
              const highTemp = (gpu.temperature_c || 0) >= 85;
              return (
                <article className={styles.gpuCard} key={gpu.id}>
                  <div className={styles.gpuHeader}><div><span>AMD GPU {gpu.id}</span><h3>{valueOrDash(gpu.utilization_percent, '%')} 使用率</h3></div><span className={highTemp ? styles.warningChip : styles.healthyChip}>{highTemp ? '溫度偏高' : '運作正常'}</span></div>
                  <MetricBar label="運算使用率" value={gpu.utilization_percent} suffix="%" />
                  <MetricBar label="VRAM" value={gpu.vram_percent} suffix="%" tone="lavender" />
                  <div className={styles.gpuFacts}>
                    <div><small>核心溫度</small><strong>{valueOrDash(gpu.temperature_c, '°C')}</strong></div>
                    <div><small>板卡功耗</small><strong>{valueOrDash(gpu.power_w, 'W')}</strong></div>
                    <div><small>VRAM 已用</small><strong>{gpu.vram_used_mb === null ? '—' : (gpu.vram_used_mb / 1024).toFixed(1) + ' GB'}</strong></div>
                    <div><small>不可修正 ECC</small><strong>{number.format(gpu.ecc_uncorrectable)}</strong></div>
                  </div>
                </article>
              );
            })}
            {!loading && !((gpuSnapshot ?? overview?.gpu)?.devices || []).length ? <article className={styles.emptyPanel}>目前沒有可用的 AMD GPU 指標。</article> : null}
          </div>

          <div className={styles.timelineGrid}>
            <GpuTimelineChart points={gpuSnapshot?.history?.points || []} metric="utilization_percent" title="GPU 運算使用率" color="#3d8f78" />
            <GpuTimelineChart points={gpuSnapshot?.history?.points || []} metric="vram_percent" title="VRAM 使用率" color="#9a78b3" />
          </div>

          <article className={styles.tablePanel}>
            <div className={styles.panelTitle}>
              <div><span className={styles.kicker}>PROCESS OWNERSHIP</span><h3>各程式 GPU 資源占用</h3></div>
              <small>{gpuSnapshot?.history?.processes?.length || 0} 個觀測程序</small>
            </div>
            <div className={styles.tableWrap}>
              <table>
                <thead><tr><th>程式／任務</th><th>PID</th><th>目前 VRAM</th><th>峰值 VRAM</th><th>首次觀測</th><th>最後觀測</th><th>辨識範圍</th></tr></thead>
                <tbody>
                  {(gpuSnapshot?.history?.processes || []).map((process) => (
                    <tr key={String(process.pid) + '-' + process.name}>
                      <td><strong>{process.name || '未知程式'}</strong></td>
                      <td>{process.pid ?? '—'}</td>
                      <td>{formatBytes(process.latest_vram_mb * 1024 * 1024)}</td>
                      <td>{formatBytes(process.max_vram_mb * 1024 * 1024)}</td>
                      <td>{formatGpuDateTime(process.first_seen)}</td>
                      <td>{formatGpuDateTime(process.last_seen)}</td>
                      <td><span className={process.namespace_limited ? styles.pendingBadge : styles.activeBadge}>{process.namespace_limited ? '容器可見' : '主機程序'}</span></td>
                    </tr>
                  ))}
                  {!gpuSnapshot?.history?.processes?.length ? <tr><td colSpan={7} className={styles.emptyCell}>目前沒有 GPU 程序；啟動推理任務後會在 5 秒內出現。</td></tr> : null}
                </tbody>
              </table>
            </div>
          </article>
        </section>

          
        <section id="data-health" className={styles.section}>
          <div className={styles.sectionHeading}>
            <div><span className={styles.kicker}>DATABASE</span><h2>資料庫健康與真實資料量</h2></div>
            <span className={dataHealth?.postgres?.available && dataHealth?.neo4j?.available ? styles.connected : styles.disconnected}>{dataLoading ? '讀取中' : 'PostgreSQL + Neo4j'}</span>
          </div>
          <div className={styles.dataGrid}>
            <article className={styles.healthCard}>
              <div className={styles.panelTitle}><div><span className={styles.kicker}>POSTGRESQL</span><h3>會員與營運資料</h3></div><b>{dataHealth?.postgres?.available ? '正常' : '未連線'}</b></div>
              <div className={styles.healthStats}>
                <div><small>資料庫容量</small><strong>{formatBytes(Number(dataHealth?.postgres?.summary?.size_bytes || 0))}</strong></div>
                <div><small>目前連線</small><strong>{number.format(Number(dataHealth?.postgres?.summary?.connections || 0))}</strong></div>
                <div><small>快取命中</small><strong>{displayValue(dataHealth?.postgres?.summary?.cache_hit_percent)}%</strong></div>
              </div>
              <div className={styles.miniList}>
                {(dataHealth?.postgres?.tables || []).slice(0, 10).map((table) => <div key={table.name}><span>{table.name}</span><b>{number.format(table.estimated_rows)} 筆 · {formatBytes(table.size_bytes)}</b></div>)}
              </div>
            </article>
            <article className={styles.healthCard}>
              <div className={styles.panelTitle}><div><span className={styles.kicker}>NEO4J</span><h3>知識圖譜</h3></div><b>{dataHealth?.neo4j?.available ? '正常' : '未連線'}</b></div>
              <div className={styles.healthStats}>
                <div><small>節點</small><strong>{number.format(dataHealth?.neo4j?.total_nodes || 0)}</strong></div>
                <div><small>關聯</small><strong>{number.format(dataHealth?.neo4j?.total_relations || 0)}</strong></div>
                <div><small>節點類型</small><strong>{number.format(dataHealth?.neo4j?.nodes?.length || 0)}</strong></div>
              </div>
              <div className={styles.miniList}>
                {(dataHealth?.neo4j?.nodes || []).slice(0, 10).map((item) => <div key={item.label}><span>{item.label}</span><b>{number.format(item.count)} 筆</b></div>)}
              </div>
            </article>
          </div>
        </section>

        <section id="knowledge" className={styles.section}>
          <div className={styles.sectionHeading}>
            <div><span className={styles.kicker}>KNOWLEDGE GRAPH</span><h2>完整知識庫與關聯資料</h2></div>
            <span className={styles.resultCount}>{number.format(knowledge?.total || 0)} 筆</span><a href="/admin/content" style={{ display: "inline-flex", alignItems: "center", gap: 10, padding: "11px 18px", borderRadius: 999, background: "#6e4c78", color: "#fff", fontWeight: 800, textDecoration: "none", boxShadow: "0 7px 18px rgba(110, 76, 120, .24)" }}>開啟完整知識控制台 <span aria-hidden="true" style={{ fontSize: 20 }}>→</span></a>
              <a href="/admin/costs" style={{ display: "inline-flex", alignItems: "center", gap: 10, padding: "11px 18px", borderRadius: 999, background: "#3b846d", color: "#fff", fontWeight: 800, textDecoration: "none", boxShadow: "0 7px 18px rgba(59,132,109,.2)" }}>API 與工具成本中心 <span aria-hidden="true">→</span></a>
          </div>
          <article className={styles.tablePanel}>
            <div className={styles.tabRow}>
              {[
                ['products', '商品'], ['ingredients', '成分'], ['regulations', '法規'],
                ['risk_rules', '風險規則'], ['faqs', 'FAQ'], ['skin_concerns', '肌膚問題'],
                ['categories', '分類'], ['relations', '所有關聯'],
              ].map(([value, label]) => (
                <button key={value} type="button" className={knowledgeEntity === value ? styles.tabActive : styles.tabButton} onClick={() => { setKnowledgeEntity(value); setKnowledgeSearch(''); void loadKnowledge(value, '', 1); }}>{label}</button>
              ))}
            </div>
            <form className={styles.searchBar} onSubmit={(event) => { event.preventDefault(); void loadKnowledge(knowledgeEntity, knowledgeSearch, 1); }}>
              <label><span>⌕</span><input value={knowledgeSearch} onChange={(event) => setKnowledgeSearch(event.target.value)} placeholder="搜尋名稱、成分、法規或關聯" /></label>
              <button type="submit">搜尋</button>
            </form>
            <div className={styles.recordList}>
              {(knowledge?.items || []).map((item) => (
                <article className={styles.recordCard} key={item.id}>
                  <div>
                    <small>{item.relation || knowledgeEntity.toUpperCase()}</small>
                    <h3>{item.display_name || (item.source_name && item.target_name ? item.source_name + ' → ' + item.target_name : item.id)}</h3>
                    {item.relation ? <p>{(item.source_labels || []).join(', ')} → {(item.target_labels || []).join(', ')}</p> : null}
                  </div>
                  <div className={styles.recordFields}>
                    {Object.entries(item.data || {}).slice(0, 10).map(([key, value]) => <span key={key}><b>{key}</b>{displayValue(value).slice(0, 180)}</span>)}
                  </div>
                </article>
              ))}
              {!knowledgeLoading && !knowledge?.items.length ? <div className={styles.emptyPanel}>找不到符合的知識資料。</div> : null}
            </div>
            <footer className={styles.pagination}><span>第 {knowledge?.page || 1} 頁，共 {Math.max(1, Math.ceil((knowledge?.total || 0) / (knowledge?.limit || 20)))} 頁</span><div><button type="button" disabled={!knowledge || knowledge.page <= 1 || knowledgeLoading} onClick={() => void loadKnowledge(knowledgeEntity, knowledgeSearch, (knowledge?.page || 1) - 1)}>上一頁</button><button type="button" disabled={!knowledge || knowledge.page * knowledge.limit >= knowledge.total || knowledgeLoading} onClick={() => void loadKnowledge(knowledgeEntity, knowledgeSearch, (knowledge?.page || 1) + 1)}>下一頁</button></div></footer>
          </article>
        </section>

        <section id="users" className={styles.section}>
          <div className={styles.sectionHeading}>
            <div><span className={styles.kicker}>MEMBERS</span><h2>會員與資源管理</h2></div>
            <span className={styles.resultCount}>{number.format(users.total)} 位會員</span>
          </div>
          <article className={styles.tablePanel}>
            <form className={styles.searchBar} onSubmit={submitSearch}>
              <label><span>⌕</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜尋帳號、名稱或 Email" /></label>
              <button type="submit">搜尋</button>
            </form>
            <div className={styles.tableWrap}>
              <table>
                <thead><tr><th>會員</th><th>狀態</th><th>登入方式</th><th>資源</th><th>最近登入</th><th>管理</th></tr></thead>
                <tbody>
                  {users.items.map((user) => (
                    <tr key={user.id}>
                      <td><div className={styles.member}><span>{(user.display_name || user.username || user.email || '?').slice(0, 1).toUpperCase()}</span><div><strong>{user.display_name || user.username || '未命名會員'}</strong><small>{user.email || '@' + (user.username || '—')}</small></div></div></td>
                      <td><span className={user.status === 'active' ? styles.activeBadge : user.status === 'suspended' ? styles.suspendedBadge : styles.pendingBadge}>{user.status === 'active' ? '使用中' : user.status === 'suspended' ? '已停用' : '待確認'}</span></td>
                      <td><div className={styles.providers}>{user.providers.length ? user.providers.map((provider) => <span key={provider}>{provider}</span>) : <span>password</span>}</div></td>
                      <td><div className={styles.resourceLine}><span>對話 {user.resources.conversations}</span><span>肌膚 {user.resources.skin_records}</span><span>簽到 {user.resources.daily_checkins}</span></div></td>
                      <td><span className={styles.dateText}>{formatDate(user.last_login_at)}</span></td>
                      <td><div className={styles.inlineActions}><button className={styles.detailButton} type="button" onClick={() => void openUserDetail(user)}>詳情</button><button className={user.status === 'suspended' ? styles.restoreButton : styles.suspendButton} type="button" onClick={() => void changeStatus(user)}>{user.status === 'suspended' ? '恢復' : '停用'}</button></div></td>
                    </tr>
                  ))}
                  {!usersLoading && !users.items.length ? <tr><td colSpan={6} className={styles.emptyCell}>找不到符合的會員</td></tr> : null}
                </tbody>
              </table>
            </div>
            <footer className={styles.pagination}><span>第 {users.page} 頁，共 {Math.max(1, Math.ceil(users.total / users.limit))} 頁</span><div><button type="button" disabled={users.page <= 1 || usersLoading} onClick={() => void loadUsers(search, users.page - 1)}>上一頁</button><button type="button" disabled={users.page * users.limit >= users.total || usersLoading} onClick={() => void loadUsers(search, users.page + 1)}>下一頁</button></div></footer>
            {selectedUser ? <div className={styles.detailPanel}>
              <div className={styles.panelTitle}><div><span className={styles.kicker}>MEMBER DETAIL</span><h3>{selectedUser.display_name || selectedUser.username || selectedUser.email || '會員詳情'}</h3></div><button className={styles.detailButton} type="button" onClick={() => { setSelectedUser(null); setUserDetail(null); }}>關閉</button></div>
              {detailLoading ? <p>讀取會員完整資料中…</p> : null}
              {userDetail ? <>
                <div className={styles.detailGrid}>
                  <div><small>帳號狀態</small><strong>{displayValue(userDetail.profile.status)}</strong></div>
                  <div><small>建立時間</small><strong>{formatDate((userDetail.profile.created_at as string | null) || null)}</strong></div>
                  <div><small>最近登入</small><strong>{formatDate((userDetail.profile.last_login_at as string | null) || null)}</strong></div>
                                    <div><small>登入方式</small><strong>{Array.isArray(userDetail.profile.providers) ? userDetail.profile.providers.join('、') : displayValue(userDetail.profile.providers)}</strong></div>
                </div>
                <div className={styles.recentGrid}>
                  {Object.entries(userDetail.recent).map(([group, records]) => <section key={group}><h4>{recentGroupLabel(group)}</h4><div className={styles.miniList}>{records.length ? records.map((record, index) => <div key={String(record.id || index)}><span>{displayValue(record.title || record.concern || record.quiz_key || record.checkin_date || '紀錄')}</span><b>{formatDate(((record.last_message_at || record.created_at || record.started_at) as string | null) || null)}</b></div>) : <div><span>目前沒有資料</span></div>}</div></section>)}
                </div>
              </> : null}
            </div> : null}
          </article>
        </section>

        <section id="observability" className={styles.section}>
          <div className={styles.sectionHeading}><div><span className={styles.kicker}>OBSERVABILITY</span><h2>錯誤與技術監控</h2></div></div>
          <div className={styles.integrationGrid}>
            <a className={styles.integrationCard} href={overview?.links.errors || '#'} target="_blank" rel="noreferrer"><span className={styles.integrationIcon}>!</span><div><small>ERROR TRACKING</small><h3>前後端錯誤事件</h3><p>例外、堆疊與發生頻率</p></div><b>{overview?.telemetry.errors_configured ? '已連線 ↗' : '未設定'}</b></a>
            <a className={styles.integrationCard} href={overview?.links.logs || '#'} target="_blank" rel="noreferrer"><span className={styles.integrationIcon}>≡</span><div><small>TECHNICAL LOGS</small><h3>服務技術 Logs</h3><p>警告、錯誤與 Request ID</p></div><b>{overview?.telemetry.logs_configured ? '已連線 ↗' : '未設定'}</b></a>
            <article className={styles.integrationCard}><span className={styles.integrationIcon}>↝</span><div><small>AI TRACE</small><h3>AI 執行軌跡</h3><p>檢索、工具、模型階段與耗時</p></div><b>{overview?.telemetry.tracing_enabled ? '已啟用' : '待啟用'}</b></article>
            <article className={styles.integrationCard}><span className={styles.integrationIcon}>✓</span><div><small>PRIVACY</small><h3>低負擔模式</h3><p>不收集聊天內容，Replay 關閉</p></div><b>保護中</b></article>
          </div>
        </section>
      </div>
    </main>
  );
}
