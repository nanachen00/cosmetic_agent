import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const read = path => readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
const compile = source => ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022, jsx: ts.JsxEmit.React },
}).outputText;
const pageSource = read("app/chat/page.tsx");
const page = ts.createSourceFile("page.tsx", pageSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const start = page.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "normalizeMessageLinks");
const end = page.statements.find(node => ts.isVariableStatement(node) && node.declarationList.declarations.some(item => item.name.getText(page) === "ReadableAssistantMessage"));
assert.ok(start && end);
const context = { React, memo: React.memo, Fragment: React.Fragment, URL, exports: {} };
vm.runInNewContext(compile(pageSource.slice(start.getStart(page), end.end)) +
  "\nexports.renderer = ReadableAssistantMessage; exports.sanitize = sanitizeRecommendationProducts; exports.find = findRecommendationEvidence;", context);
const { renderer, sanitize, find } = context.exports;
const render = (content, products) => renderToStaticMarkup(React.createElement(renderer, { content, products }));
const reply = "**✔️以自動套用你的偏好**\n- 預算500元｜無香料\n**適合你的原因**\n本次先比較保濕\n1. 保濕精華\n品牌：合成品牌\n價格：NT$450\n適合你的原因：這段是回覆文字的舊理由\n重點成分：甘油\n查看商品資料：https://example.org/product";
const fixture = {
  id: "synthetic-product-a", name: "合成品牌 保濕精華", brand: "合成品牌",
  personalization_policy_version: "personal-v3",
  personalization_reasons: ["符合你500元以內的預算", "符合你偏好的清爽質地", "不應顯示的第三理由"],
  personalization_missing: ["尚未確認目前售價", "不應顯示的第二缺口"],
  recommendation_limitations: "只核對官方主打成分，未完成完整INCI。",
  inci_status: "missing", ingredient_evidence_level: "official_highlights",
  price_observed_at: "2026-11-17T00:00:00Z", price_source_url: "https://example.org/price",
  personalization_score: 0.91,
};
const products = sanitize([fixture]);
assert.equal(products.length, 1);
assert.equal(products[0].personalization_reasons.length, 2);
assert.equal(products[0].personalization_missing.length, 1);
assert.equal("personalization_score" in products[0], false, "Scalar scores are not a calibrated fit percentage");
assert.equal(sanitize([null, 1, {}, { name: 3 }]).length, 0);

const legacy = render(reply);
assert.match(legacy, /本次需求摘要（請確認）/);
assert.doesNotMatch(legacy, /✔️|自動套用|已套用/);
assert.doesNotMatch(legacy, /<span>依你偏好<\/span>/);
assert.match(legacy, /這段是回覆文字的舊理由/);
assert.match(legacy, /未提供價格更新時間/);
assert.match(legacy, /href="https:\/\/example.org\/product"[^>]*rel="noopener noreferrer"[^>]*target="_blank"/);
assert.doesNotMatch(render("**已自動套用你的資料**\n乾性肌"), /已自動套用/,
  "Incomplete streams and non-card replies cannot assert application via a heading either");

assert.doesNotMatch(render(reply.replace("✔️以自動套用你的偏好", "已套用你的偏好")), /已套用你的偏好/);

const trusted = render(reply, products);
assert.match(trusted, /<span>依你偏好<\/span>/);
assert.match(trusted, /符合你500元以內的預算/);
assert.match(trusted, /符合你偏好的清爽質地/);
assert.doesNotMatch(trusted, /不應顯示|這段是回覆文字的舊理由|91%|0\.91/);
assert.equal((trusted.match(/<span>資料待確認<\/span>/g) ?? []).length, 1);
assert.match(trusted, /尚未確認目前售價/);
assert.match(trusted, /未完成完整INCI/);
assert.match(trusted, /價格觀察：2026-11-17/);
assert.match(trusted, /href="https:\/\/example.org\/price"[^>]*target="_blank"[^>]*rel="noopener noreferrer"/);

const other = sanitize([{ ...fixture, name: "不同精華" }]);
assert.doesNotMatch(render(reply, other), /<span>依你偏好<\/span>/, "No array-position matching");
assert.equal(find("保濕精華", "合成品牌", sanitize([fixture, { ...fixture, id: "other-id" }])), undefined,
  "Duplicate names cannot borrow one product's evidence");
assert.equal(find("保濕精華", "不同品牌", products), undefined, "Brand mismatch cannot borrow evidence");
assert.doesNotMatch(render(reply, sanitize([{ ...fixture, personalization_policy_version: "unknown-version" }])), /<span>依你偏好<\/span>/);
const unsafe = render(reply, sanitize([{ ...fixture, personalization_reasons: ["<img src=x onerror=alert(1)>"], price_source_url: "javascript:alert(1)", price_observed_at: "2026-99-99" }]));
assert.match(unsafe, /&lt;img/);
assert.doesNotMatch(unsafe, /<img|href="javascript:|價格觀察：2026-99-99/);

assert.equal((pageSource.match(/products: sanitizeRecommendationProducts\(res\.products\)/g) ?? []).length, 3,
  "Stream, quick reply, and recovered job completions all retain bounded evidence");
assert.match(pageSource, /return \{ \.\.\.message, products: sanitizeRecommendationProducts\(message\.products\) \};/,
  "Restored history has the same bounded evidence handling");
assert.match(pageSource, /<ReadableAssistantMessage content=\{message\.content\} products=\{message\.products\} \/>/,
  "Memoized formatter receives stable message-owned evidence");

const productsPage = read("app/products/page.tsx");
const demoContext = {
  React, useState: React.useState, useEffect: React.useEffect, useMemo: React.useMemo,
  Icon: () => null, PageHeader: props => React.createElement("header", null, props.description), exports: {},
};
const demoSource = productsPage.slice(productsPage.indexOf("const products =")).replace("export default function ProductsPage", "function ProductsPage");
vm.runInNewContext(compile(demoSource) + "\nexports.Page = ProductsPage;", demoContext);
const demo = renderToStaticMarkup(React.createElement(demoContext.exports.Page));
assert.match(demo, /固定虛構資料/);
assert.match(demo, /未讀取你的個人資料/);
assert.match(demo, /示範商品/);
assert.ok(demo.indexOf("示範資料") < demo.indexOf("product-grid"), "Demonstration notice must appear before product results");
assert.doesNotMatch(demo, /\d+%|AI MATCH RESULT|PERSONAL FIT|MATCH/);
assert.doesNotMatch(productsPage, /match:\s*\d|selectedProduct\.match|product\.match/);

const quiz = read("components/CustomerQuiz.tsx");
assert.match(quiz, /已儲存這次測驗結果/);
assert.match(quiz, /隨時更正推薦條件/);
assert.doesNotMatch(quiz, /實際瀏覽|購買行為持續更新偏好/);
assert.match(quiz, /result\.persisted === false/);
assert.match(quiz, /尚未寫入會員資料/);
console.log("Recommendation truthfulness: SSR evidence gating, identity matching, bounded reasons, safe rendering, demo disclosure and quiz wording passed.");
