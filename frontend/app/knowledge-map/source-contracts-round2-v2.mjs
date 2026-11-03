import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const read = (path) => readFileSync(new URL(`../${path}`, import.meta.url), "utf8");

const home = read("app/page.tsx");
assert.doesNotMatch(home, /navigator\.geolocation/, "首頁不得要求定位權限");
assert.match(home, /latitude: "25\.0330"/, "首頁天氣應固定使用臺北預設座標");
assert.match(home, /location: "臺北市"/, "首頁天氣應明確顯示臺北市");

const quizPage = read("app/personality-quiz/page.tsx");
assert.match(quizPage, /登入會員即可同步到基本資料/, "人格測驗說明須區分訪客結果");
assert.match(quizPage, /← 返回首頁/, "訪客可直接返回首頁");

const quiz = read("components/CustomerQuiz.tsx");
assert.match(quiz, /result\.persisted === false/, "結果頁須依是否持久化分流");
assert.match(quiz, /完成並返回首頁/, "訪客結果須返回首頁");
assert.match(quiz, /完成並返回基本資料/, "會員結果須返回基本資料");

const checkin = read("app/daily-checkin/page.tsx");
const unauthenticatedBlock = checkin.match(/if \(!identity\) \{[\s\S]*?return;[\s\S]*?\}/)?.[0] ?? "";
assert.ok(unauthenticatedBlock, "簽到頁須有未登入保護");
assert.doesNotMatch(unauthenticatedBlock, /setLoaded\(true\)/, "登入導轉前不得閃出零值簽到卡");
assert.match(checkin, /正在確認會員與簽到資料/, "簽到頁須顯示安全載入狀態");
assert.doesNotMatch(checkin, /Failed to load reviewed daily content/, "備援每日內容不得製造錯誤噪音");

const knowledgeMapCss = read("app/knowledge-map/KnowledgeMap.module.css");
assert.match(
  knowledgeMapCss,
  /\.node\s*\{[\s\S]*?overflow:\s*hidden;/,
  "knowledge-map bubbles must clip all visual content to the circle",
);
assert.match(
  knowledgeMapCss,
  /\.node strong\s*\{[\s\S]*?-webkit-line-clamp:\s*2;/,
  "outer knowledge-map labels must stay within two lines",
);
assert.match(
  knowledgeMapCss,
  /\.centerNode strong\s*\{[\s\S]*?-webkit-line-clamp:\s*3;/,
  "center knowledge-map labels must stay within three lines",
);

const knowledgeMapPage = read("app/knowledge-map/page.tsx");
assert.match(
  knowledgeMapPage,
  /nodeDisplayLabel\(node: PositionedNode\)/,
  "knowledge-map bubbles must render a compact node label",
);
assert.match(
  knowledgeMapPage,
  /setGraph\(nextGraph\);\s*setSelectedId\(null\);/,
  "knowledge-map queries must start with no relationship popover selected",
);
assert.match(
  knowledgeMapPage,
  /\{selectedNode \|\| selectedEdge \? \(\s*<aside className=\{styles\.graphPopover\}/,
  "knowledge relationship summary must render inside the graph only after selection",
);
assert.doesNotMatch(
  knowledgeMapPage,
  /detailHandle|detailExpanded|上滑|收合成摘要/,
  "knowledge-map must not use an AI-like swipe-up detail sheet",
);
assert.match(
  knowledgeMapPage,
  /正在整理這個關係/,
  "knowledge relationship popover must show a stable loading state",
);
assert.match(
  knowledgeMapPage,
  /\/api\/v1\/profile[\s\S]*?viewer_context/,
  "knowledge explanation must use the signed-in viewer profile when available",
);
assert.doesNotMatch(
  knowledgeMapPage,
  /查看資料來源|個資料來源|來源定位|最近審核/,
  "knowledge detail must keep internal evidence metadata out of the visitor UI",
);
assert.doesNotMatch(
  knowledgeMapPage,
  />已審核關係</,
  "knowledge detail must not expose internal review jargon",
);
assert.match(
  knowledgeMapPage,
  /explanationSections\[0\]\?\.content[\s\S]*?slice\(0, 88\)/,
  "knowledge-map must reduce long explanations to a concise popover summary",
);
assert.match(
  knowledgeMapPage,
  /<line[\s\S]*?className=\{styles\.edgeLine\}[\s\S]*?x1=\{lineX1\}[\s\S]*?x2=\{lineX2\}/,
  "knowledge-map relationships must render as thin straight lines",
);
assert.match(
  knowledgeMapCss,
  /\.edgeLine\s*\{[\s\S]*?stroke-width:\s*1;/,
  "knowledge-map straight connectors must stay visually thin",
);
assert.doesNotMatch(
  knowledgeMapPage,
  /curvePath|edgeCurve|<span className=\{styles\.detailSticker\}>/,
  "knowledge-map must not show curved connectors or the internal entity sticker",
);
assert.match(
  knowledgeMapPage,
  /<section className=\{styles\.header\}[\s\S]*?<div className=\{styles\.workspace\}>[\s\S]*?<section[\s\S]*?className=\{styles\.graphPanel\}/,
  "knowledge-map search and graph must share one stable page",
);
assert.doesNotMatch(
  knowledgeMapPage,
  /returnToSearch|resultsBack|!graph\s*\?\s*\([\s\S]*?styles\.header/,
  "knowledge-map must not switch to a separate long result page",
);
assert.match(
  knowledgeMapPage,
  /點選圈圈，直接查看關係摘要/,
  "knowledge-map graph must explain its direct-on-canvas interaction",
);
assert.doesNotMatch(
  knowledgeMapPage,
  /圖中關係|relationSummary/,
  "knowledge-map must not repeat graph edges in a relationship strip",
);
assert.match(
  knowledgeMapPage,
  /useEffect\(\(\) => \{\s*const previousBodyOverflow[\s\S]*?document\.body\.style\.overflow = "hidden";[\s\S]*?document\.documentElement\.style\.overflow = "hidden";[\s\S]*?\}, \[\]\);/,
  "knowledge-map must lock document scrolling in both search and result states",
);
assert.match(
  knowledgeMapCss,
  /\.page\s*\{[\s\S]*?height:\s*calc\(100dvh - 36px\);[\s\S]*?display:\s*flex;[\s\S]*?overflow:\s*hidden;/,
  "knowledge-map whole page must fit inside the visible viewport",
);
assert.match(
  knowledgeMapPage,
  /className=\{styles\.graphCanvas\}[\s\S]*?onClick=\{closeSelection\}[\s\S]*?event\.stopPropagation\(\);[\s\S]*?selectNode\(node\.id\)/,
  "knowledge-map must close the popover on empty-canvas click without swallowing node clicks",
);
assert.match(
  knowledgeMapPage,
  /\{ id: "concern", label: "肌膚狀況" \}[\s\S]*?\{ id: "care", label: "照護需求" \}/,
  "knowledge-map search must expose clear topic categories",
);
assert.match(
  knowledgeMapPage,
  /className=\{styles\.quickSuggestions\}[\s\S]*?categoryConfig\.examples\.map[\s\S]*?runQuery\(example\.query\)/,
  "knowledge-map must keep visible, actionable suggestion tags",
);
assert.match(
  knowledgeMapPage,
  /input_was_rewritten[\s\S]*?已替你找到：\{graph\.resolution\.interpreted_query \|\| graph\.center\.name\}/,
  "knowledge-map must disclose how a natural-language or misspelled query was resolved",
);
assert.match(
  knowledgeMapPage,
  /knowledge_term_not_indexed[\s\S]*?圖譜關聯還在補充中/,
  "knowledge-map must distinguish understood-but-unindexed topics from unrelated input",
);
assert.match(
  knowledgeMapPage,
  /knowledge_query_out_of_scope[\s\S]*?美妝與肌膚知識/,
  "knowledge-map must give out-of-scope queries a specific, friendly response",
);
assert.doesNotMatch(
  knowledgeMapPage,
  /setLoading\(true\);[\s\S]{0,180}setGraph\(null\);/,
  "knowledge-map must keep the previous graph visible while a replacement query loads",
);
assert.doesNotMatch(
  knowledgeMapPage,
  /<section\s+className=\{styles\.graphPanel\}[^>]*aria-live=/,
  "knowledge-map must not make assistive technology re-announce the entire graph",
);
assert.match(
  knowledgeMapCss,
  /\.header h1\s*\{[\s\S]*?font-size:\s*26px;[\s\S]*?font-weight:\s*800;/,
  "knowledge-map title hierarchy must match the homepage's bold rounded typography",
);
assert.match(
  knowledgeMapCss,
  /\.quickSuggestions span\s*\{[\s\S]*?background:\s*#d8eee5;[\s\S]*?font-size:\s*10px;/,
  "knowledge-map suggestion labels must use the homepage mint accent and readable small type",
);
assert.doesNotMatch(
  knowledgeMapPage,
  /皮爽/,
  "knowledge-map quick exploration must not promote a niche drug brand",
);
assert.doesNotMatch(
  knowledgeMapPage,
  /精華液|乳液|洗面乳/,
  "knowledge-map quick exploration must use mappable knowledge entities instead of broad product types",
);
assert.doesNotMatch(
  knowledgeMapPage,
  /<small>\{nodeTypeLabel\(node\)\}<\/small>/,
  "knowledge-map bubbles must keep only the concise entity name",
);
assert.match(
  knowledgeMapPage,
  /className=\{styles\.graphLegend\}[\s\S]*?visibleNodeTypes/,
  "knowledge-map node types must move into a compact color legend",
);
assert.match(
  knowledgeMapPage,
  /styles\.relatedEdge[\s\S]*?styles\.dimmedEdge[\s\S]*?styles\.dimmedNode/,
  "knowledge-map selection must emphasize the active relationship path",
);
assert.doesNotMatch(
  knowledgeMapCss,
  /graphPanel::before[\s\S]*?radial-gradient\(circle at 14px 14px/,
  "knowledge-map graph canvas must not reuse the dense dotted background",
);
assert.match(
  knowledgeMapPage,
  /LOW_VALUE_NODE_NAME[\s\S]*?LOW_VALUE_NODE_NAME\.test\(node\.name\.trim\(\)\)/,
  "knowledge-map must filter low-value instruction-only nodes from exploration",
);
assert.match(
  knowledgeMapCss,
  /\.graphPanel\s*\{[\s\S]*?border:\s*0;[\s\S]*?radial-gradient[\s\S]*?box-shadow:\s*none;/,
  "knowledge-map result must use the plain skin-report surface without another card",
);
assert.doesNotMatch(
  knowledgeMapPage,
  /styles\.edgeLabel|<rect x=\{-labelWidth/,
  "knowledge-map must not add redundant label pills over relationship lines",
);
assert.match(
  knowledgeMapCss,
  /\.graphPopover\s*\{[\s\S]*?position:\s*absolute;[\s\S]*?width:\s*min\(340px,[\s\S]*?backdrop-filter:\s*blur\(16px\);/,
  "knowledge-map relationship summary must be a compact on-canvas popover",
);

console.log("Frontend source contracts passed");
