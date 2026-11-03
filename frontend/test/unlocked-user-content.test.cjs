const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const read = (relativePath) =>
  fs.readFileSync(path.join(root, relativePath), "utf8");

test("daily knowledge cards are always visible and the consultation is clickable", () => {
  const source = read("app/daily-checkin/page.tsx");

  assert.doesNotMatch(
    source,
    /beautyUnlocked|doctorUnlocked|BEAUTY_UNLOCK_DAY|DOCTOR_UNLOCK_DAY|TESTING_UNLOCK_ALL|locked-overlay|is-locked|永久解鎖|還差\s*\{/
  );
  assert.match(source, /className="daily-knowledge-content"/);
  assert.match(source, /className="daily-knowledge-content doctor-card-content"/);
  assert.match(source, /onClick=\{\s*startSkinConsult\s*\}/);
  assert.match(source, /開始膚況諮詢/);
});

test("skin report has no streak or eligibility display gate", () => {
  const source = read("app/skin-report/page.tsx");

  assert.doesNotMatch(source, /REPORT_ACCESS_ALWAYS_ENABLED/);
  assert.doesNotMatch(source, /eligibility\.days_needed|未達五天|再記錄.*天即可產生分析報告/);
  assert.match(source, /payload && selectedSummary \? \(/);
  assert.match(source, /error\.status === 401 \|\| error\.status === 403/);
});

test("legacy API lock and eligibility fields remain type-compatible", () => {
  const checkinApi = read("lib/checkin-api.ts");
  const trackingTypes = read("types/skin-tracking.ts");

  for (const field of [
    "beauty_unlocked",
    "doctor_unlocked",
    "beauty_remaining",
    "doctor_remaining",
  ]) {
    assert.match(checkinApi, new RegExp(field + ":"));
  }
  assert.match(trackingTypes, /interface SkinReportEligibility/);
  assert.match(trackingTypes, /eligibility: SkinReportEligibility/);
  assert.match(trackingTypes, /report: SkinReportData \| null/);
});

test("product lock styling is gone while security and operation locks remain", () => {
  const css = read("app/globals.css");
  const accountSecurity = read("components/AccountSecurityPanel.tsx");
  const daily = read("app/daily-checkin/page.tsx");

  assert.doesNotMatch(css, /unlockable-content|unlockable-card|locked-overlay|unlock-copy/);
  assert.match(css, /\.daily-knowledge-content/);
  assert.match(accountSecurity, /usernameChangeLocked/);
  assert.match(accountSecurity, /unlinkWouldRemoveLastLogin/);
  assert.match(daily, /disabled=\{\s*!loaded \|\|/);
});
