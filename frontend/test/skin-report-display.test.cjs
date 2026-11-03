const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const ts = require("typescript");

const source = fs.readFileSync(path.join(__dirname, "../lib/skin-report.ts"), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const loaded = { exports: {} };
new Function("module", "exports", "require", compiled)(loaded, loaded.exports, require);
const { buildHistoryRecords, buildRegionMapTags, buildRegionSummaries } = loaded.exports;

const left = {
  record_id: "left-001", date: "2026-09-26", zone: "left_cheek",
  issue: "acne_scar", severity: 2, note: "左邊有淡淡痘印",
  display_note: "有淡淡痘印", products: [], record_status: "recorded",
  stability_source: null,
};
const right = {
  record_id: "right-001", date: "2026-09-26", zone: "right_cheek",
  issue: "acne_scar", severity: 5,
  note: "右邊一個禮拜了還是很明顯",
  display_note: "痘疤已持續一個禮拜，仍然明顯",
  products: [], record_status: "recorded", stability_source: null,
};

function regionItems(records, zone) {
  return buildHistoryRecords(records)[0].regions.find((region) => region.region === zone).issues;
}

test("daily report keeps left and right wording separate", () => {
  assert.deepEqual(regionItems([left, right], "left_cheek"), [
    "痘疤，程度輕微", "有淡淡痘印",
  ]);
  assert.deepEqual(regionItems([left, right], "right_cheek"), [
    "痘疤，程度嚴重", "痘疤已持續一個禮拜，仍然明顯",
  ]);
});

test("missing model text falls back to the original note", () => {
  assert.deepEqual(regionItems([{ ...left, display_note: null }], "left_cheek"), [
    "痘疤，程度輕微", "左邊有淡淡痘印",
  ]);
});

test("locked region summary also shows display-only wording", () => {
  const summaries = buildRegionSummaries({ report: null, records: [left, right] });
  const leftSummary = summaries.find((region) => region.region === "left_cheek");
  assert.deepEqual(leftSummary.recentRecords[0].items, [
    "痘疤，程度輕微", "有淡淡痘印",
  ]);
});

test("other records use a concrete wrinkle tag", () => {
  const wrinkle = { ...left, zone: "t_zone", issue: "other", severity: 2,
    tags: [{ slug: "deep_wrinkles", display_name: "皺紋" }],
    note: "皺紋很多", display_note: null };
  assert.deepEqual(regionItems([wrinkle], "t_zone"), ["皺紋，程度輕微", "皺紋很多"]);
});

test("black circles keep left and right report regions separate", () => {
  const tag = { slug: "periorbital_darkening", display_name: "黑眼圈" };
  const eyeLeft = { ...left, issue: "other", severity: 3, tags: [tag],
    note: "左邊黑眼圈很重", display_note: "黑眼圈很重" };
  const eyeRight = { ...right, issue: "other", severity: 3, tags: [tag],
    note: "右邊黑眼圈很重", display_note: "黑眼圈很重" };
  assert.deepEqual(regionItems([eyeLeft, eyeRight], "left_cheek"),
    ["黑眼圈，程度中度", "黑眼圈很重"]);
  assert.deepEqual(regionItems([eyeLeft, eyeRight], "right_cheek"),
    ["黑眼圈，程度中度", "黑眼圈很重"]);
});

test("older other records without tags use their note once", () => {
  const legacy = { ...left, issue: "other", severity: 2, tags: null,
    note: "皺紋很多", display_note: null };
  assert.deepEqual(regionItems([legacy], "left_cheek"), ["皺紋很多，程度輕微"]);
});

test("other records without a tag or note never show the generic label", () => {
  const empty = { ...left, issue: "other", severity: 2, tags: null,
    note: "", display_note: null };
  assert.deepEqual(regionItems([empty], "left_cheek"), ["已記錄膚況，程度輕微"]);
});

test("unlocked region summary names the concrete condition", () => {
  const wrinkle = { ...left, issue: "other", tags: [{ slug: "deep_wrinkles", display_name: "皺紋" }] };
  const summary = buildRegionSummaries({ report: null, records: [wrinkle] })
    .find((region) => region.region === "left_cheek");
  assert.equal(summary.mainCondition, "皺紋");
});

test("report-backed region summary names the concrete condition", () => {
  const wrinkle = { ...left, issue: "other", tags: [{ slug: "deep_wrinkles", display_name: "皺紋" }] };
  const report = { zones: [{ zone: "left_cheek", top_issue: "other",
    record_count: 1, trend: "stable" }], summary_text: "", consecutive_days: 1 };
  const summary = buildRegionSummaries({ report, records: [wrinkle] })[0];
  assert.equal(summary.mainCondition, "皺紋");
});

test("selected-range focus uses the concrete condition", () => {
  const wrinkle = { ...left, issue: "other", tags: [{ slug: "deep_wrinkles", display_name: "皺紋" }] };
  const tips = loaded.exports.buildSelectedRangeCareTips([wrinkle]);
  assert.match(tips[0], /皺紋/);
  assert.doesNotMatch(tips[0], /其他膚況/);
});

test("map tags keep a formal issue visible for legacy records without tag metadata", () => {
  const legacy = {
    record_id: "legacy-itch-001",
    date: "2026-09-28",
    zone: "t_zone",
    issue: "itching",
    severity: 2,
    tags: null,
    subregions: [{ slug: "glabella", display_name: "眉心" }],
  };
  const tags = buildRegionMapTags([legacy], "t_zone");
  assert.deepEqual(
    tags.map(({ subregionLabel, displayName }) => ({ subregionLabel, displayName })),
    [{ subregionLabel: "眉心", displayName: "搔癢" }],
  );
});

test("map tags are de-duplicated and capped for readability", () => {
  const records = Array.from({ length: 6 }, (_, index) => ({
    record_id: `itch-${index}`,
    date: `2026-09-${String(28 - index).padStart(2, "0")}`,
    zone: "t_zone",
    issue: index < 2 ? "itching" : "dryness",
    severity: 2,
    tags: null,
    subregions: [{ slug: `zone-${index}`, display_name: `部位${index}` }],
  }));
  const tags = buildRegionMapTags(records, "t_zone", 4);
  assert.equal(tags.length, 4);
});
