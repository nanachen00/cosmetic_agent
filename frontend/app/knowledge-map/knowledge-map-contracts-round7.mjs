import assert from "node:assert/strict";
import fs from "node:fs";

const page = fs.readFileSync("app/knowledge-map/page.tsx", "utf8");
const css = fs.readFileSync("app/knowledge-map/KnowledgeMap.module.css", "utf8");

assert.match(page, /getCurrentSkinReport\(30\)/, "must read recent skin records");
assert.match(page, /reportViewerContext/, "must convert report data into bounded context");
assert.match(page, /viewer_context: viewerContext/, "must send context to query and explainer");
assert.match(page, /data-personalization/, "must expose personalized graph state");
assert.match(page, /已依你的膚況與紀錄排序/, "must explain personalized ordering");
assert.match(page, /近期變化/, "must surface worsening concerns before generic examples");
assert.match(css, /node\[data-personalization="priority"\]/, "must style personal relevance");
assert.match(css, /node\[data-personalization="caution"\]/, "must style personal caution");
assert.match(css, /personalizedReason/, "must explain why a node is personal");

console.log("knowledge-map round7 personalization contracts passed");
