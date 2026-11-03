"use client";

import Link from "next/link";
import {
  type CSSProperties,
  type FormEvent,
  type PointerEvent as ReactPointerEvent,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { Icon } from "@/components/Icon";
import { API_ORIGIN } from "@/lib/api-origin";

import styles from "./KnowledgeMap.module.css";

type KnowledgeNode = {
  id: string;
  name: string;
  type?: string;
  description?: string;
  properties?: Record<string, unknown>;
};

type KnowledgeEdge = {
  id?: string;
  source?: string | { id?: string };
  target?: string | { id?: string };
  source_id?: string;
  target_id?: string;
  from?: string;
  to?: string;
  relation?: string;
  relation_label?: string;
  type?: string;
  label?: string;
  evidence_status?: string;
  evidence_reference?: string;
  source_ids?: string[];
  source_locator?: string;
  reviewed_at?: string;
};

type KnowledgeGraph = {
  center: KnowledgeNode;
  nodes: KnowledgeNode[];
  edges: KnowledgeEdge[];
  resolution?: {
    query?: string;
    strategy?: string;
  };
  evidence?: {
    edge_ids?: string[];
    source_ids?: string[];
    statuses?: string[];
  };
  explanation?: {
    explanation?: string;
  };
};

type PositionedNode = KnowledgeNode & {
  x: number;
  y: number;
  isCenter: boolean;
};

type PositionedEdge = {
  id: string;
  source: PositionedNode;
  target: PositionedNode;
  label: string;
  evidenceStatus?: string;
  evidenceReference?: string;
  sourceIds: string[];
  sourceLocator?: string;
  reviewedAt?: string;
};

type PhysicsNode = PositionedNode & {
  vx: number;
  vy: number;
};

type CanvasSize = {
  width: number;
  height: number;
};

type KnowledgeCategory = "all" | "ingredient" | "concern" | "care" | "drug";

type QuickExample = {
  query: string;
  tag: string;
};

type CategoryConfig = {
  label: string;
  description: string;
  placeholder: string;
  examples: QuickExample[];
};

type ExplanationFocus = "overview" | "cause" | "care" | "safety";

type ViewerContext = {
  skin_type?: string;
  concerns?: string[];
  allergens?: string[];
};

type ExplanationResponse = {
  explanation?: string;
  sections?: Array<{ title?: string; content?: string }>;
  personalized?: boolean;
  personalization_used?: string[];
  focus?: ExplanationFocus;
};

const CATEGORY_OPTIONS: Array<{ id: KnowledgeCategory; label: string }> = [
  { id: "all", label: "全部" },
  { id: "ingredient", label: "成分" },
  { id: "concern", label: "肌膚狀況" },
  { id: "care", label: "照護需求" },
  { id: "drug", label: "藥品" },
];

const CATEGORY_CONFIGS: Record<KnowledgeCategory, CategoryConfig> = {
  all: {
    label: "全部",
    description: "選擇已收錄的知識實體，看懂成分、肌膚狀況、照護需求與藥品之間的關聯。",
    placeholder: "例如：閉鎖性粉刺、水楊酸、阿達帕林",
    examples: [
      { query: "閉鎖性粉刺", tag: "膚況" },
      { query: "水楊酸", tag: "成分" },
      { query: "溫和清潔", tag: "照護" },
      { query: "阿達帕林", tag: "藥品" },
    ],
  },
  ingredient: {
    label: "成分",
    description: "從已收錄成分開始，看懂主要作用與可連結的照護方向。",
    placeholder: "例如：水楊酸、BHA",
    examples: ["水楊酸", "BHA"].map((query) => ({ query, tag: "成分" })),
  },
  concern: {
    label: "肌膚狀況",
    description: "從已收錄膚況開始，整理外觀、形成原因、照護方向與注意事項。",
    placeholder: "例如：閉鎖性粉刺、內包粉刺、白頭粉刺",
    examples: ["閉鎖性粉刺", "內包粉刺", "白頭粉刺"].map((query) => ({ query, tag: "膚況" })),
  },
  care: {
    label: "照護需求",
    description: "以已建立關係的照護需求為起點，連回適用膚況、作用與注意事項。",
    placeholder: "例如：溫和清潔、不致粉刺保養、疏通毛孔",
    examples: ["溫和清潔", "不致粉刺保養", "疏通毛孔"].map((query) => ({ query, tag: "照護" })),
  },
  drug: {
    label: "藥品",
    description: "從已收錄藥品成分或別名開始，查看作用與安全提醒。",
    placeholder: "例如：阿達帕林、Adapalene、外用維 A 酸",
    examples: ["阿達帕林", "Adapalene", "外用維 A 酸"].map((query) => ({ query, tag: "藥品" })),
  },
};

const PROFILE_SKIN_TYPE_LABELS: Record<string, string> = {
  oily: "油性",
  dry: "乾性",
  normal: "中性",
  combination: "混合性",
  sensitive: "敏感性",
};

function profileViewerContext(value: unknown): ViewerContext | null {
  if (!value || typeof value !== "object") return null;
  const profile = value as Record<string, unknown>;
  const skinTypeValue = profile.skinType ?? profile.skin_type;
  const rawSkinType = typeof skinTypeValue === "string" ? skinTypeValue.trim().slice(0, 32) : "";
  const skinType = PROFILE_SKIN_TYPE_LABELS[rawSkinType.toLowerCase()] ?? rawSkinType;
  const list = (item: unknown) => Array.isArray(item)
    ? Array.from(new Set(item
      .filter((entry): entry is string => typeof entry === "string")
      .map((entry) => entry.trim().slice(0, 32))
      .filter(Boolean)))
      .slice(0, 5)
    : [];
  const concerns = list(profile.concerns ?? profile.skinConcerns ?? profile.skin_concerns);
  const allergens = list(profile.allergens ?? profile.dislikedIngredients ?? profile.disliked_ingredients);
  if (!skinType && concerns.length === 0 && allergens.length === 0) return null;
  return {
    ...(skinType ? { skin_type: skinType } : {}),
    ...(concerns.length ? { concerns } : {}),
    ...(allergens.length ? { allergens } : {}),
  };
}

const DESKTOP_VISIBLE_NODES = 8;
const MOBILE_VISIBLE_NODES = 5;
const PUBLIC_EVIDENCE_STATUSES = new Set(["verified", "official-regulatory"]);
const LOW_VALUE_NODE_NAME = /依(?:產品)?標示(?:循序)?使用|依指示|按產品標示/;
function endpointId(value: KnowledgeEdge["source"]): string {
  if (typeof value === "string") return value;
  if (value && typeof value === "object" && typeof value.id === "string") {
    return value.id;
  }
  return "";
}

function edgeSourceId(edge: KnowledgeEdge): string {
  return edge.source_id ?? edge.from ?? endpointId(edge.source);
}

function edgeTargetId(edge: KnowledgeEdge): string {
  return edge.target_id ?? edge.to ?? endpointId(edge.target);
}

const RELATION_LABELS: Record<string, string> = {
  ALIAS_OF: "又稱",
  HAS_ALIAS: "別名",
  HAS_SYMPTOM: "常見症狀",
  HAS_TRIGGER: "常見誘因",
  SUPPORTS_NEED: "支援需求",
  HAS_FUNCTION: "主要作用",
  REQUIRES_FUNCTION: "需要作用",
  SUITABLE_FOR: "適用於",
  MAY_IRRITATE: "可能刺激",
  INTERACTS_WITH: "搭配注意",
  ACTIVE_INGREDIENT_OF: "有效成分",
  ACTIVE_IN: "為有效成分",
  BELONGS_TO_CATEGORY: "屬於",
  BELONGS_TO_DRUG_CLASS: "藥物分類",
  REQUIRES_CAUTION: "使用注意",
  RELEVANT_TO_CARE: "照護方向",
  USE_WITH_CAUTION_FOR: "此膚況需謹慎",
  NAVIGATES_TO_CONCERN: "可探索膚況",
  NAVIGATES_TO_INGREDIENT: "可探索成分",
  RELATED_CONCERN: "相關膚況",
  SUITABLE_FOR_CONCERN: "適合膚況",
  MAY_REFER_TO: "可能指稱",
  MANUFACTURED_BY: "製造商",
  CONTAINS_INGREDIENT: "含有成分",
  HAS_EVIDENCE: "資料來源",
};

const NODE_TYPE_LABELS: Record<string, string> = {
  Ingredient: "成分",
  SkinConcern: "膚況",
  RiskRule: "風險規則",
  Function: "作用",
  CareNeed: "照護需求",
  ConcernCategory: "膚況分類",
  AliasTerm: "別名",
  DrugClass: "藥物類別",
  DrugProduct: "藥品",
  DrugActiveIngredient: "藥品成分",
  Product: "保養品",
  ProductCategory: "保養品類型",
  CosmeticProduct: "保養品",
  Manufacturer: "製造商",
  Symptom: "症狀",
  Trigger: "誘因",
  SafetyWarning: "安全提醒",
};

function nodeTypeLabel(node: PositionedNode): string {
  return NODE_TYPE_LABELS[node.type ?? ""] ?? (node.isCenter ? "搜尋主題" : "關聯節點");
}

function nodeDisplayLabel(node: PositionedNode): string {
  const explicit = node.properties?.short_label;
  const rawName = typeof explicit === "string" && explicit.trim()
    ? explicit.trim()
    : node.name.replace(/（[^）]*）|\([^)]*\)/g, "").trim();
  const readableShortcuts: Array<[RegExp, string]> = [
    [/溫和清潔與不致粉刺保養/, "溫和清潔"],
    [/毛囊被皮脂與老廢角質堵塞/, "毛孔堵塞"],
    [/膚色或白色(?:的)?小凸起/, "白色小凸起"],
    [/避免摳抓或擠壓/, "避免擠壓"],
    [/疏通堵塞毛孔/, "疏通毛孔"],
    [/去除老廢角質/, "代謝角質"],
  ];
  const shortcut = readableShortcuts.find(([pattern]) => pattern.test(rawName));
  const raw = shortcut?.[1] ?? rawName;
  const maxLength = node.isCenter ? 7 : 5;
  return raw.length > maxLength ? raw.slice(0, maxLength - 1) + "…" : raw;
}

function edgeLabel(edge: KnowledgeEdge): string {
  const raw = edge.relation_label ?? edge.label ?? edge.relation ?? edge.type;
  if (!raw) return "";
  return RELATION_LABELS[raw] ?? raw.replaceAll("_", " ");
}

function uniqueNodes(graph: KnowledgeGraph, limit: number): KnowledgeNode[] {
  const seen = new Set<string>();
  const centerId = graph.center.id;
  const outer = (Array.isArray(graph.nodes) ? graph.nodes : [])
    .filter((node): node is KnowledgeNode => {
      if (!node || typeof node.id !== "string" || typeof node.name !== "string") return false;
      if (node.id === centerId || seen.has(node.id)) return false;
      if (LOW_VALUE_NODE_NAME.test(node.name.trim())) return false;
      seen.add(node.id);
      return true;
    });
  return [graph.center, ...outer].slice(0, limit);
}

function seedNodes(graph: KnowledgeGraph, size: CanvasSize, limit: number): PositionedNode[] {
  const nodes = uniqueNodes(graph, limit);
  const center = nodes[0];
  const outer = nodes.slice(1);
  const mobile = size.width <= 700;
  const centerX = size.width / 2;
  const centerY = size.height * (mobile ? 0.47 : 0.5);
  const radiusX = Math.max(82, Math.min(size.width * 0.34, size.width / 2 - 44));
  const radiusY = Math.max(88, Math.min(size.height * 0.27, size.height / 2 - 48));
  return [
    { ...center, x: centerX, y: centerY, isCenter: true },
    ...outer.map((node, index) => {
      const angle = -Math.PI / 2 + (index / Math.max(1, outer.length)) * Math.PI * 2;
      const stagger = index % 2 === 0 ? 1 : 0.86;
      return {
        ...node,
        x: centerX + Math.cos(angle) * radiusX * stagger,
        y: centerY + Math.sin(angle) * radiusY * stagger,
        isCenter: false,
      };
    }),
  ];
}

function positionEdges(graph: KnowledgeGraph, nodes: PositionedNode[]): PositionedEdge[] {
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const seenPairs = new Set<string>();
  return (Array.isArray(graph.edges) ? graph.edges : [])
    .map<PositionedEdge | null>((edge, index) => {
      const source = byId.get(edgeSourceId(edge));
      const target = byId.get(edgeTargetId(edge));
      const label = edgeLabel(edge);
      if (!source || !target || !label) return null;
      const pairKey = [source.id, target.id].sort().join("::");
      if (seenPairs.has(pairKey)) return null;
      seenPairs.add(pairKey);
      return {
        id: edge.id ?? source.id + ":" + target.id + ":" + index,
        source,
        target,
        label,
        evidenceStatus: edge.evidence_status,
        evidenceReference: edge.evidence_reference,
        sourceIds: Array.isArray(edge.source_ids) ? edge.source_ids : [],
        sourceLocator: edge.source_locator,
        reviewedAt: edge.reviewed_at,
      };
    })
    .filter((edge): edge is PositionedEdge => edge !== null);
}

function useForceGraph(graph: KnowledgeGraph | null) {
  const canvasRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<number | null>(null);
  const physicsRef = useRef<PhysicsNode[]>([]);
  const dragRef = useRef<{ id: string; moved: boolean; lastX: number; lastY: number } | null>(null);
  const didDragRef = useRef(false);
  const runRef = useRef<() => void>(() => undefined);
  const [size, setSize] = useState<CanvasSize>({ width: 0, height: 0 });
  const [nodes, setNodes] = useState<PositionedNode[]>([]);
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [reducedMotion, setReducedMotion] = useState(false);
  const [settled, setSettled] = useState(false);

  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReducedMotion(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    const element = canvasRef.current;
    if (!element) return;
    const update = () => {
      setSize({ width: Math.max(1, element.clientWidth), height: Math.max(1, element.clientHeight) });
    };
    update();
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, [graph]);

  useEffect(() => {
    if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
    frameRef.current = null;
    if (!graph || size.width <= 1 || size.height <= 1) {
      physicsRef.current = [];
      setNodes([]);
      setSettled(false);
      return;
    }

    const limit = size.width <= 700 ? MOBILE_VISIBLE_NODES : DESKTOP_VISIBLE_NODES;
    const seeded = seedNodes(graph, size, limit);
    physicsRef.current = seeded.map((node) => ({ ...node, vx: 0, vy: 0 }));
    setNodes(seeded);
    setSettled(reducedMotion);
    if (reducedMotion) return;
    const settleFallback = window.setTimeout(() => setSettled(true), 900);

    const run = () => {
      if (frameRef.current !== null) return;
      let ticks = 0;
      let quietTicks = 0;
      const tick = () => {
        frameRef.current = null;
        const current = physicsRef.current;
        const byId = new Map(current.map((node) => [node.id, node]));
        const mobile = size.width <= 700;
        const centerX = size.width / 2;
        const centerY = size.height * (mobile ? 0.47 : 0.5);
        const draggedId = dragRef.current?.id;

        for (const node of current) {
          if (node.isCenter) {
            node.x = centerX;
            node.y = centerY;
            node.vx = 0;
            node.vy = 0;
          } else if (node.id !== draggedId) {
            node.vx += (centerX - node.x) * 0.0014;
            node.vy += (centerY - node.y) * 0.0014;
          }
        }

        for (const edge of graph.edges ?? []) {
          const source = byId.get(edgeSourceId(edge));
          const target = byId.get(edgeTargetId(edge));
          if (!source || !target) continue;
          const dx = target.x - source.x;
          const dy = target.y - source.y;
          const distance = Math.max(1, Math.hypot(dx, dy));
          const desired = mobile ? 128 : 168;
          const pull = (distance - desired) * 0.006;
          const fx = (dx / distance) * pull;
          const fy = (dy / distance) * pull;
          if (!source.isCenter && source.id !== draggedId) {
            source.vx += fx;
            source.vy += fy;
          }
          if (!target.isCenter && target.id !== draggedId) {
            target.vx -= fx;
            target.vy -= fy;
          }
        }

        for (let leftIndex = 0; leftIndex < current.length; leftIndex += 1) {
          for (let rightIndex = leftIndex + 1; rightIndex < current.length; rightIndex += 1) {
            const left = current[leftIndex];
            const right = current[rightIndex];
            let dx = right.x - left.x;
            let dy = right.y - left.y;
            let distance = Math.hypot(dx, dy);
            if (distance < 0.01) {
              dx = (rightIndex % 2 ? 1 : -1) * 0.1;
              dy = 0.1;
              distance = Math.hypot(dx, dy);
            }
            const minDistance = (left.isCenter ? (mobile ? 56 : 64) : (mobile ? 39 : 46))
              + (right.isCenter ? (mobile ? 56 : 64) : (mobile ? 39 : 46))
              + (mobile ? 14 : 18);
            const collision = Math.max(0, minDistance - distance) * 0.085;
            const repulsion = Math.min(0.5, 900 / (distance * distance));
            const force = collision + repulsion;
            const fx = (dx / distance) * force;
            const fy = (dy / distance) * force;
            if (!left.isCenter && left.id !== draggedId) {
              left.vx -= fx;
              left.vy -= fy;
            }
            if (!right.isCenter && right.id !== draggedId) {
              right.vx += fx;
              right.vy += fy;
            }
          }
        }

        let energy = 0;
        for (const node of current) {
          if (node.isCenter || node.id === draggedId) continue;
          node.vx *= 0.86;
          node.vy *= 0.86;
          node.x += node.vx;
          node.y += node.vy;
          const radius = mobile ? 39 : 46;
          node.x = Math.max(radius, Math.min(size.width - radius, node.x));
          node.y = Math.max(radius, Math.min(size.height - radius, node.y));
          energy += Math.abs(node.vx) + Math.abs(node.vy);
        }

        setNodes(current.map(({ vx: _vx, vy: _vy, ...node }) => node));
        ticks += 1;
        quietTicks = energy < 0.18 ? quietTicks + 1 : 0;
        if (ticks < 210 && quietTicks < 16) {
          frameRef.current = requestAnimationFrame(tick);
        } else {
          setSettled(true);
        }
      };
      frameRef.current = requestAnimationFrame(tick);
    };

    runRef.current = run;
    run();
    return () => {
      window.clearTimeout(settleFallback);
      if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    };
  }, [graph, reducedMotion, size.height, size.width]);

  function resetLayout() {
    if (!graph || size.width <= 1 || size.height <= 1) return;
    const limit = size.width <= 700 ? MOBILE_VISIBLE_NODES : DESKTOP_VISIBLE_NODES;
    const seeded = seedNodes(graph, size, limit);
    physicsRef.current = seeded.map((node) => ({ ...node, vx: 0, vy: 0 }));
    setNodes(seeded);
    setSettled(reducedMotion);
    if (!reducedMotion) runRef.current();
  }

  function pointerDown(event: ReactPointerEvent<HTMLButtonElement>, node: PositionedNode) {
    if (node.isCenter) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = { id: node.id, moved: false, lastX: event.clientX, lastY: event.clientY };
    didDragRef.current = false;
    setDraggingId(node.id);
    if (frameRef.current !== null) cancelAnimationFrame(frameRef.current);
    frameRef.current = null;
  }

  function pointerMove(event: ReactPointerEvent<HTMLButtonElement>, node: PositionedNode) {
    const drag = dragRef.current;
    const canvas = canvasRef.current;
    if (!drag || drag.id !== node.id || !canvas) return;
    const rect = canvas.getBoundingClientRect();
    const target = physicsRef.current.find((item) => item.id === node.id);
    if (!target) return;
    const movement = Math.hypot(event.clientX - drag.lastX, event.clientY - drag.lastY);
    if (movement > 2) {
      drag.moved = true;
      setSettled(false);
    }
    const scaleX = rect.width > 0 ? canvas.clientWidth / rect.width : 1;
    const scaleY = rect.height > 0 ? canvas.clientHeight / rect.height : 1;
    target.vx = (event.clientX - drag.lastX) * scaleX * 0.28;
    target.vy = (event.clientY - drag.lastY) * scaleY * 0.28;
    target.x = Math.max(42, Math.min(canvas.clientWidth - 42, (event.clientX - rect.left) * scaleX));
    drag.lastX = event.clientX;
    drag.lastY = event.clientY;
    setNodes(physicsRef.current.map(({ vx: _vx, vy: _vy, ...item }) => item));
  }

  function pointerUp(event: ReactPointerEvent<HTMLButtonElement>, node: PositionedNode) {
    const drag = dragRef.current;
    if (!drag || drag.id !== node.id) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
    didDragRef.current = drag.moved;
    dragRef.current = null;
    setDraggingId(null);
    if (!reducedMotion && drag.moved) {
      runRef.current();
      window.setTimeout(() => setSettled(true), 700);
    }
  }

  return { canvasRef, nodes, size, settled, draggingId, didDragRef, resetLayout, pointerDown, pointerMove, pointerUp };
}

function validateGraph(value: unknown): KnowledgeGraph {
  if (!value || typeof value !== "object") {
    throw new Error("knowledge_response_invalid");
  }
  const graph = value as Partial<KnowledgeGraph>;
  if (
    !graph.center ||
    typeof graph.center.id !== "string" ||
    typeof graph.center.name !== "string"
  ) {
    throw new Error("knowledge_response_invalid");
  }
  const edges = (Array.isArray(graph.edges) ? graph.edges : []).filter((edge) =>
    PUBLIC_EVIDENCE_STATUSES.has(String(edge.evidence_status ?? "")),
  );
  const visibleNodeIds = new Set([
    graph.center.id,
    ...edges.flatMap((edge) => [edgeSourceId(edge), edgeTargetId(edge)]),
  ]);
  const nodes = (Array.isArray(graph.nodes) ? graph.nodes : []).filter((node) =>
    node && typeof node.id === "string" && visibleNodeIds.has(node.id),
  );
  return { ...graph, center: graph.center, nodes, edges };
}

class KnowledgeMapRequestError extends Error {
  constructor(readonly status: number, readonly code: string) {
    super(code || "knowledge_http_" + status);
  }
}

async function requestFailure(response: Response): Promise<KnowledgeMapRequestError> {
  const body = await response.json().catch(() => null);
  const code = typeof body?.detail?.code === "string" ? body.detail.code : "";
  return new KnowledgeMapRequestError(response.status, code);
}

function friendlyError(status: number, code = ""): string {
  if (code === "knowledge_graph_disabled" || code === "knowledge_query_disabled") {
    return "知識地圖尚未啟用；不是你輸入的名詞有問題。";
  }
  if (status === 404) {
    return "目前沒有收錄這個名詞，請換個名稱或常見別名試試。";
  }
  if (status === 401 || status === 403) {
    return "目前沒有權限讀取知識地圖，請確認登入狀態。";
  }
  if (status === 422) return "請輸入 1 到 160 字的保養相關名詞。";
  if (code.endsWith("_unavailable") || code === "embedding_unavailable" || code === "reranker_not_configured") {
    return "知識資料或語意服務暫時無法使用，請稍後再試；不是查無此名詞。";
  }
  if (status === 429 || status === 503) {
    return "目前查詢人數較多，請稍後再試一次。";
  }
  return "知識地圖暫時無法連線，請稍後再試。";
}

export default function KnowledgeMapPage() {
  const [query, setQuery] = useState("");
  const [activeCategory, setActiveCategory] = useState<KnowledgeCategory>("all");
  const categoryConfig = CATEGORY_CONFIGS[activeCategory];
  const [graph, setGraph] = useState<KnowledgeGraph | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [explanation, setExplanation] = useState("");
  const [explanationSections, setExplanationSections] = useState<Array<{ title: string; content: string }>>([]);
  const [explanationLoading, setExplanationLoading] = useState(false);
  const [viewerContext, setViewerContext] = useState<ViewerContext | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const explanationAbortRef = useRef<AbortController | null>(null);
  const requestIdRef = useRef(0);

  useEffect(() => () => {
    abortRef.current?.abort();
    explanationAbortRef.current?.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void fetch(`${API_ORIGIN}/api/v1/profile`, {
      credentials: "include",
      signal: controller.signal,
    })
      .then(async (response) => response.ok ? response.json() as Promise<{ profile?: unknown }> : null)
      .then((body) => {
        if (!controller.signal.aborted) setViewerContext(profileViewerContext(body?.profile));
      })
      .catch(() => {
        if (!controller.signal.aborted) setViewerContext(null);
      });
    return () => controller.abort();
  }, []);

  useEffect(() => {
    const previousBodyOverflow = document.body.style.overflow;
    const previousRootOverflow = document.documentElement.style.overflow;
    const previousBodyOverscroll = document.body.style.overscrollBehavior;
    document.body.style.overflow = "hidden";
    document.documentElement.style.overflow = "hidden";
    document.body.style.overscrollBehavior = "none";
    return () => {
      document.body.style.overflow = previousBodyOverflow;
      document.documentElement.style.overflow = previousRootOverflow;
      document.body.style.overscrollBehavior = previousBodyOverscroll;
    };
  }, []);


  const {
    canvasRef,
    nodes,
    size,
    draggingId,
    didDragRef,
    resetLayout,
    pointerDown,
    pointerMove,
    pointerUp,
  } = useForceGraph(graph);
  const edges = useMemo(
    () => (graph ? positionEdges(graph, nodes) : []),
    [graph, nodes],
  );
  const selectedNode = nodes.find((node) => node.id === selectedId) ?? null;
  const selectedEdge = edges.find((edge) => edge.id === selectedEdgeId) ?? null;
  const selectedEdges = selectedNode
    ? edges.filter((edge) => edge.source.id === selectedNode.id || edge.target.id === selectedNode.id)
    : [];
  const highlightedNodeIds = useMemo(() => {
    const highlighted = new Set<string>();
    if (selectedNode) {
      highlighted.add(selectedNode.id);
      edges.forEach((edge) => {
        if (edge.source.id === selectedNode.id) highlighted.add(edge.target.id);
        if (edge.target.id === selectedNode.id) highlighted.add(edge.source.id);
      });
    } else if (selectedEdge) {
      highlighted.add(selectedEdge.source.id);
      highlighted.add(selectedEdge.target.id);
    }
    return highlighted;
  }, [edges, selectedEdge, selectedNode]);
  const visibleNodeTypes = useMemo(() => {
    const byLabel = new Map<string, string>();
    nodes.forEach((node) => {
      const label = nodeTypeLabel(node);
      if (!byLabel.has(label)) byLabel.set(label, (node.type ?? "entity").toLowerCase());
    });
    return Array.from(byLabel, ([label, kind]) => ({ label, kind })).slice(0, 4);
  }, [nodes]);
  async function loadExplanation(nodeId: string, focus: ExplanationFocus = "overview") {
    explanationAbortRef.current?.abort();
    const controller = new AbortController();
    explanationAbortRef.current = controller;
    setExplanationLoading(true);
    setExplanationSections([]);
    try {
      const response = await fetch(`${API_ORIGIN}/api/knowledge/explain`, {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          node_id: nodeId,
          focus,
          ...(viewerContext ? { viewer_context: viewerContext } : {}),
        }),
        signal: controller.signal,
      });
      if (!response.ok) {
        const failure = await requestFailure(response);
        if (!controller.signal.aborted) {
          setExplanation(friendlyError(failure.status, failure.code));
          setExplanationSections([]);
        }
        return;
      }
      const body = (await response.json()) as ExplanationResponse;
      if (!controller.signal.aborted && typeof body.explanation === "string") {
        setExplanation(body.explanation);
        setExplanationSections(Array.isArray(body.sections)
          ? body.sections.flatMap((item) => {
            const title = typeof item.title === "string" ? item.title.trim() : "";
            const content = typeof item.content === "string" ? item.content.trim() : "";
            return title && content ? [{ title, content }] : [];
          }).slice(0, 4)
          : []);
      }
    } catch (requestError) {
      if ((requestError as Error).name !== "AbortError") {
        setExplanation("");
        setExplanationSections([]);
      }
    } finally {
      if (!controller.signal.aborted) setExplanationLoading(false);
    }
  }

  async function runQuery(rawQuery: string) {
    const normalized = rawQuery.trim();
    if (!normalized) {
      setError("請先輸入想探索的膚況、成分、需求或藥品名稱。");
      return;
    }

    abortRef.current?.abort();
    explanationAbortRef.current?.abort();
    const controller = new AbortController();
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    abortRef.current = controller;
    setLoading(true);
    setError("");
    setGraph(null);
    setSelectedId(null);
    setSelectedEdgeId(null);
    setExplanation("");
    setExplanationSections([]);
    setExplanationLoading(false);

    try {
      const response = await fetch(API_ORIGIN + "/api/knowledge/query", {
        method: "POST",
        credentials: "include",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: normalized, depth: 1, limit: 60, explain: false }),
        signal: controller.signal,
      });
      if (!response.ok) {
        throw await requestFailure(response);
      }
      const nextGraph = validateGraph(await response.json());
      if (requestId !== requestIdRef.current) return;
      setGraph(nextGraph);
      setSelectedId(null);
      if (nextGraph.edges.length === 0) {
        setError("目前只有基本資料，還沒有足夠關係可以整理成地圖。");
      }
    } catch (requestError) {
      if ((requestError as Error).name === "AbortError" || requestId !== requestIdRef.current) return;
      const match = (requestError as Error).message.match(/^knowledge_http_(\d+)$/);
      setGraph(null);
      setError(requestError instanceof KnowledgeMapRequestError
        ? friendlyError(requestError.status, requestError.code)
        : match ? friendlyError(Number(match[1])) : friendlyError(0));
    } finally {
      if (requestId === requestIdRef.current) {
        setLoading(false);
      }
    }
  }

  function changeCategory(nextCategory: KnowledgeCategory) {
    abortRef.current?.abort();
    explanationAbortRef.current?.abort();
    setActiveCategory(nextCategory);
    setQuery("");
    setGraph(null);
    setSelectedId(null);
    setSelectedEdgeId(null);
    setExplanation("");
    setExplanationSections([]);
    setExplanationLoading(false);
    setError("");
    setLoading(false);
  }

  function selectNode(nodeId: string) {
    if (didDragRef.current) {
      didDragRef.current = false;
      return;
    }
    setSelectedEdgeId(null);
    setSelectedId(nodeId);
    setExplanation("");
    setExplanationSections([]);
    setExplanationLoading(false);
    void loadExplanation(nodeId, "overview");
  }

  function selectEdge(edgeId: string) {
    explanationAbortRef.current?.abort();
    setSelectedId(null);
    setSelectedEdgeId(edgeId);
    setExplanation("");
    setExplanationSections([]);
    setExplanationLoading(false);
  }

  function closeSelection() {
    explanationAbortRef.current?.abort();
    setSelectedId(null);
    setSelectedEdgeId(null);
    setExplanation("");
    setExplanationSections([]);
    setExplanationLoading(false);
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void runQuery(query);
  }

  const graphLabel = graph
    ? `${graph.center.name}，${Math.max(0, nodes.length - 1)} 個關聯節點`
    : activeCategory === "all" ? "尚未建立知識地圖" : `尚未建立${categoryConfig.label}圖譜`;

  const selectedRelation = selectedEdge ?? selectedEdges[0] ?? null;
  const relatedNode = selectedNode && selectedRelation
    ? selectedRelation.source.id === selectedNode.id ? selectedRelation.target : selectedRelation.source
    : null;
  const popoverTitle = selectedEdge
    ? `${selectedEdge.source.name} × ${selectedEdge.target.name}`
    : selectedNode?.name ?? "";
  const popoverRelation = selectedEdge
    ? selectedEdge.label
    : selectedRelation && relatedNode ? `與 ${relatedNode.name} · ${selectedRelation.label}` : "關聯節點";
  const rawPopoverSummary = selectedEdge
    ? `${selectedEdge.source.name}與${selectedEdge.target.name}之間的關係是「${selectedEdge.label}」。`
    : explanationSections[0]?.content || explanation || selectedNode?.description || "點選相連的圈圈，可以繼續查看它們之間的關係。";
  const normalizedPopoverSummary = rawPopoverSummary.replace(/\s+/g, " ").trim();
  const popoverSummary = normalizedPopoverSummary.length > 88
    ? `${normalizedPopoverSummary.slice(0, 88).trim()}…`
    : normalizedPopoverSummary;

  return (
    <div className={`${styles.page} ${styles.resultMode}`}>
      <section className={styles.header} aria-label="知識地圖搜尋">
        <div className={styles.headerRow}>
          <Link className={styles.backButton} href="/" aria-label="回到首頁">
            <Icon name="arrow" />
          </Link>
          <div>
            <h1>美妝知識地圖</h1>
            <p>搜尋一個名詞，看懂它與肌膚、成分和照護的關係</p>
          </div>
        </div>

        <form className={styles.searchForm} onSubmit={submit}>
          <label className={styles.searchField}>
            <span className={styles.srOnly}>搜尋{categoryConfig.label === "全部" ? "成分、肌膚狀況、照護需求或藥品" : categoryConfig.label}</span>
            <Icon name="search" />
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder={categoryConfig.placeholder}
              autoComplete="off"
              maxLength={activeCategory === "ingredient" ? 80 : 160}
              disabled={loading}
            />
          </label>
          <button type="submit" disabled={loading}>
            {loading ? "整理中" : "搜尋"}
          </button>
        </form>

        <div className={`${styles.examples} ${styles.categoryTabs}`} aria-label="選擇知識分類">
          {CATEGORY_OPTIONS.map((category) => (
            <button
              key={category.id}
              type="button"
              aria-pressed={activeCategory === category.id}
              onClick={() => changeCategory(category.id)}
            >
              {category.label}
            </button>
          ))}
        </div>
      </section>

      <div className={styles.workspace}>
        <section
          className={styles.graphPanel}
          aria-label={graphLabel}
          aria-live="polite"
          aria-busy={loading}
        >
          {graph ? (
            <>
              <div className={styles.graphMeta}>
                <div className={styles.graphTitle}>
                  <strong>{graph.center.name}</strong>
                  <span>{Math.max(0, nodes.length - 1)} 個關聯節點</span>
                </div>
                <div className={styles.graphControls}>
                  <button type="button" onClick={resetLayout}>重設位置</button>
                </div>
              </div>

              <div className={styles.graphCanvas} ref={canvasRef} onClick={closeSelection}>
                <svg
                  className={styles.edgeLayer}
                  viewBox={"0 0 " + size.width + " " + size.height}
                  preserveAspectRatio="none"
                  role="group"
                  aria-label="節點關係"
                >
                  <defs>
                    <marker id="knowledge-map-arrow" viewBox="0 0 12 12" refX="10" refY="6" markerWidth="5" markerHeight="5" orient="auto">
                      <path d="M 2 2 L 10 6 L 2 10" />
                    </marker>
                  </defs>
                  {edges.map((edge) => {
                    const isRelatedEdge = selectedNode
                      ? edge.source.id === selectedNode.id || edge.target.id === selectedNode.id
                      : selectedEdge?.id === edge.id;
                    const dx = edge.target.x - edge.source.x;
                    const dy = edge.target.y - edge.source.y;
                    const distance = Math.max(1, Math.hypot(dx, dy));
                    const sourcePadding = edge.source.isCenter ? (size.width <= 700 ? 54 : 63) : (size.width <= 700 ? 37 : 45);
                    const targetPadding = edge.target.isCenter ? (size.width <= 700 ? 57 : 66) : (size.width <= 700 ? 40 : 48);
                    const lineX1 = edge.source.x + (dx / distance) * sourcePadding;
                    const lineY1 = edge.source.y + (dy / distance) * sourcePadding;
                    const lineX2 = edge.target.x - (dx / distance) * targetPadding;
                    const lineY2 = edge.target.y - (dy / distance) * targetPadding;
                    return (
                      <g
                        key={edge.id}
                        className={
                          styles.edgeGroup +
                          (selectedEdge?.id === edge.id ? " " + styles.selectedEdge : "") +
                          (selectedNode || selectedEdge
                            ? isRelatedEdge ? " " + styles.relatedEdge : " " + styles.dimmedEdge
                            : "")
                        }
                        data-evidence={edge.evidenceStatus ?? "unknown"}
                      >
                        <line
                          className={styles.edgeLine}
                          x1={lineX1}
                          y1={lineY1}
                          x2={lineX2}
                          y2={lineY2}
                          markerEnd="url(#knowledge-map-arrow)"
                          onClick={(event) => {
                            event.stopPropagation();
                            selectEdge(edge.id);
                          }}
                        />
                      </g>
                    );
                  })}
                </svg>

                {nodes.map((node) => (
                  <button
                    key={node.id}
                    type="button"
                    className={
                      styles.node +
                      (node.isCenter ? " " + styles.centerNode : "") +
                      (highlightedNodeIds.size > 0 && !highlightedNodeIds.has(node.id) ? " " + styles.dimmedNode : "")
                    }
                    style={
                      {
                        "--node-x": node.x + "px",
                        "--node-y": node.y + "px",
                      } as CSSProperties
                    }
                    data-kind={(node.type ?? "entity").toLowerCase()}
                    data-dragging={draggingId === node.id ? "true" : "false"}
                    aria-label={nodeTypeLabel(node) + "：" + node.name}
                    aria-pressed={selectedNode?.id === node.id}
                    onPointerDown={(event) => pointerDown(event, node)}
                    onPointerMove={(event) => pointerMove(event, node)}
                    onPointerUp={(event) => pointerUp(event, node)}
                    onPointerCancel={(event) => pointerUp(event, node)}
                    onClick={(event) => {
                      event.stopPropagation();
                      selectNode(node.id);
                    }}
                  >
                    <strong title={node.name}>{nodeDisplayLabel(node)}</strong>
                  </button>
                ))}
              </div>

              {selectedNode || selectedEdge ? (
                <aside className={styles.graphPopover} aria-label="關係摘要" onClick={(event) => event.stopPropagation()}>
                  <button type="button" className={styles.popoverClose} aria-label="關閉關係摘要" onClick={closeSelection}>×</button>
                  <span className={styles.popoverRelation}>{popoverRelation}</span>
                  <strong>{popoverTitle}</strong>
                  <p>{explanationLoading ? "正在整理這個關係…" : popoverSummary}</p>
                  {selectedNode && selectedEdges.length > 1 ? (
                    <small>另有 {selectedEdges.length - 1} 個相連關係，可點其他圈圈查看</small>
                  ) : null}
                </aside>
              ) : (
                <div className={styles.graphFooter}>
                  <div className={styles.graphLegend} aria-label="節點顏色圖例">
                    {visibleNodeTypes.map((item) => (
                      <span key={item.label}><i data-kind={item.kind} />{item.label}</span>
                    ))}
                  </div>
                  <p className={styles.graphHint}>點選圈圈，直接查看關係摘要</p>
                </div>
              )}
            </>
          ) : (
            <div className={styles.emptyState}>
              <h2>{loading ? "正在整理關係…" : "從一個你在意的詞開始"}</h2>
              <p>{activeCategory === "all" ? "例如：水楊酸、內包粉刺、溫和清潔" : `輸入${categoryConfig.label}名稱，關係會直接顯示在這裡`}</p>
              <span>內容為一般知識整理，不取代醫療診斷。</span>
            </div>
          )}

          {error ? <p className={styles.errorMessage} role="alert">{error}</p> : null}
        </section>
      </div>
    </div>
  );
}
