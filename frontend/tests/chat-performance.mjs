import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { performance } from "node:perf_hooks";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const read = path => readFileSync(new URL(`../${path}`, import.meta.url), "utf8");
const compile = (source, options = {}) => ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022, ...options },
}).outputText;
const compiled = compile(read("lib/chat-history-persistence.ts"));
const { ChatHistoryPersistence } = await import("data:text/javascript;base64," + Buffer.from(compiled).toString("base64"));

function memoryStorage() {
  return {
    values: new Map(), writes: 0,
    setItem(key, value) { this.values.set(key, value); this.writes += 1; },
  };
}
const key = "blink_chat_history:member-a:conversation-a";
const message = {
  id: "answer", role: "assistant", content: "已完成的回覆",
  cards: [{ title: "保養品", url: "https://example.org/product" }],
  quickReplies: [{ label: "繼續", value: "next" }],
};
const persistence = new ChatHistoryPersistence();
const storage = memoryStorage();
assert.equal(persistence.persist(storage, key, [message], 1000), true);
for (let frame = 0; frame < 240; frame += 1) {
  assert.equal(persistence.persist(storage, key, [message], 2000 + frame), false);
}
assert.equal(storage.writes, 1, "A new filtered array with identical committed messages must not write every stream frame");
assert.deepEqual(JSON.parse(storage.values.get(key)), { version: 1, expiresAt: 1000, messages: [message] });

const edited = { ...message, content: "更新後的完整回覆" };
assert.equal(persistence.persist(storage, key, [edited], 3000), true, "Changed content with the same message id must be persisted");
assert.equal(JSON.parse(storage.values.get(key)).messages[0].content, edited.content);
const cancelled = { id: "cancelled", role: "assistant", content: "部分回覆\n\n（已取消生成）" };
assert.equal(persistence.persist(storage, key, [edited, cancelled], 4000), true);
assert.deepEqual(JSON.parse(storage.values.get(key)).messages, [edited, cancelled], "A reload must retain committed answers, cards and cancelled partial content");
assert.equal(persistence.persist(storage, key, [], 5000), true, "Clearing history is also a committed change");
assert.deepEqual(JSON.parse(storage.values.get(key)).messages, []);

const isolated = new ChatHistoryPersistence();
for (const isolatedKey of [key, "blink_chat_history:member-b:conversation-a", "blink_chat_history:member-a:conversation-b", key]) {
  assert.equal(isolated.persist(storage, isolatedKey, [message], 6000), true);
}
const otherSession = memoryStorage();
assert.equal(isolated.persist(otherSession, key, [message], 6000), true, "A different session storage must not inherit another session's write cache");
assert.equal(otherSession.writes, 1);
const reloadedPage = new ChatHistoryPersistence();
assert.equal(reloadedPage.persist(otherSession, key, [message], 7000), true, "A fresh page must not skip its initial persisted snapshot");

let failNextWrite = true;
const unreliable = {
  writes: 0,
  setItem() { if (failNextWrite) { failNextWrite = false; throw new Error("quota"); } this.writes += 1; },
};
const retryable = new ChatHistoryPersistence();
assert.throws(() => retryable.persist(unreliable, key, [message], 1000), /quota/);
assert.equal(retryable.persist(unreliable, key, [message], 1000), true);
assert.equal(retryable.persist(unreliable, key, [message], 1000), false);
assert.equal(unreliable.writes, 1, "A failed storage write must remain retryable");
const unserializable = { ...message, toJSON() { throw new Error("serialization"); } };
assert.throws(() => retryable.persist(unreliable, key, [unserializable], 1000), /serialization/);
assert.equal(retryable.persist(unreliable, key, [message, cancelled], 1000), true);

// Evaluate the actual pure message formatter without mounting a browser or the chat page.
const pageSource = read("app/chat/page.tsx");
const page = ts.createSourceFile("page.tsx", pageSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let persistenceEffect;
function findPersistenceEffect(node) {
  if (ts.isCallExpression(node) && node.expression.getText(page) === "useEffect" && node.arguments[0]?.getText(page).includes("historyPersistenceRef.current!.persist(")) {
    persistenceEffect = node.arguments[0];
  }
  ts.forEachChild(node, findPersistenceEffect);
}
findPersistenceEffect(page);
assert.ok(persistenceEffect, "Exercise the live effect rather than only its helper");
const effectStorage = memoryStorage();
const cacheErrors = [];
const effectContext = {
  historyLoaded: true, userId: "member-b", conversationId: "conversation-a",
  loadedConversationId: "conversation-a", loadedHistoryKey: key,
  chatHistoryKey: "blink_chat_history:member-b:conversation-a",
  messages: [message], sessionStorage: effectStorage,
  historyPersistenceRef: { current: new ChatHistoryPersistence() },
  isVisibleChatMessage: item => item.visible !== false,
  CHAT_HISTORY_TTL_MS: 1800000, Date: { now: () => 100 },
  console: { error: (...args) => cacheErrors.push(args[0]) },
};
vm.runInNewContext(compile(`globalThis.runPersistenceEffect = ${persistenceEffect.getText(page)};`), effectContext);
effectContext.runPersistenceEffect();
assert.equal(effectStorage.writes, 0, "A member change with the same conversation id must not cache the previous member's messages before the new history is loaded");
effectContext.loadedHistoryKey = effectContext.chatHistoryKey;
effectContext.historyLoaded = false;
effectContext.runPersistenceEffect();
assert.equal(effectStorage.writes, 0, "A load in progress must not overwrite restored history");
effectContext.historyLoaded = true;
effectContext.messages = [edited, { id: "hidden", role: "user", content: "internal", visible: false }];
for (let frame = 0; frame < 240; frame += 1) {
  effectContext.messages = [...effectContext.messages.filter(item => item.id !== "loading-indicator"), { id: "loading-indicator", role: "assistant", content: `partial ${frame}` }];
  effectContext.runPersistenceEffect();
}
assert.equal(effectStorage.writes, 1, "The actual stream effect must skip identical committed histories");
assert.deepEqual(JSON.parse(effectStorage.values.get(effectContext.chatHistoryKey)).messages, [edited]);
effectContext.messages = [edited, cancelled];
effectContext.runPersistenceEffect();
assert.equal(effectStorage.writes, 2);
assert.deepEqual(JSON.parse(effectStorage.values.get(effectContext.chatHistoryKey)).messages, [edited, cancelled]);
effectContext.sessionStorage = { setItem() { throw new Error("quota"); } };
effectContext.runPersistenceEffect();
assert.deepEqual(cacheErrors, ["Save chat history failed:"], "Storage errors must remain handled by the live effect");

const formatterStart = page.statements.find(statement => ts.isFunctionDeclaration(statement) && statement.name?.text === "normalizeMessageLinks");
const formatterEnd = page.statements.find(statement => ts.isVariableStatement(statement) && statement.declarationList.declarations.some(declaration => declaration.name.getText(page) === "ReadableAssistantMessage"));
assert.ok(formatterStart && formatterEnd, "The pure formatter must remain independently exercisable");
const formatter = pageSource.slice(formatterStart.getStart(page), formatterEnd.end);
const formatterJs = compile(formatter, { jsx: ts.JsxEmit.React });
const sandbox = { React, memo: React.memo, Fragment: React.Fragment, URL, exports: {} };
vm.runInNewContext(formatterJs + "\nexports.ReadableAssistantMessage = ReadableAssistantMessage;", sandbox);
const { ReadableAssistantMessage } = sandbox.exports;
assert.equal(ReadableAssistantMessage.$$typeof, Symbol.for("react.memo"));
assert.equal(ReadableAssistantMessage.compare, null, "Use React's default prop comparison so changed content cannot be hidden by an id-only comparator");
const plain = "第一段\n\n第二段 **重點** [資料來源](https://example.org/source)";
const plainMarkup = renderToStaticMarkup(React.createElement(ReadableAssistantMessage, { content: plain }));
assert.match(plainMarkup, /第一段<\/p><p>第二段/);
assert.match(plainMarkup, /<strong[^>]*>重點<\/strong>/);
assert.match(plainMarkup, /href="https:\/\/example.org\/source"/);
const product = "推薦內容\n1. 保濕精華\n品牌：測試品牌\n價格：NT$500\n適合你的原因：溫和保濕\n查看商品資料：https://example.org/product";
const productMarkup = renderToStaticMarkup(React.createElement(ReadableAssistantMessage, { content: product }));
assert.match(productMarkup, /product-recommendation-card/);
assert.match(productMarkup, /href="https:\/\/example.org\/product"[^>]*rel="noopener noreferrer"[^>]*target="_blank"/);
const changedMarkup = renderToStaticMarkup(React.createElement(ReadableAssistantMessage, { content: product.replace("保濕精華", "新的產品") }));
assert.match(changedMarkup, /新的產品/);
assert.notEqual(changedMarkup, productMarkup);
assert.match(pageSource, /<ReadableAssistantMessage content=\{message\.content\} products=\{message\.products\} \/>/, "The memoized formatter must receive stable content and message-owned products, without fresh callback/object props");
assert.match(pageSource, /historyPersistenceRef\.current!\.persist\(\s*sessionStorage,\s*chatHistoryKey,\s*messagesToSave,/, "The live history effect must use the tested persistence gate with its existing member/conversation key");
assert.match(pageSource, /window\.requestAnimationFrame\(\(\) => \{\s*streamUpdateFrameRef\.current = null;/, "Keep the existing frame batching");

// Synthetic immutable-state replay of the old and new persistence paths. Storage is
// an in-memory stub: these CPU numbers are not browser, network or model latency.
function replay(historySize, frames, optimized) {
  const history = Array.from({ length: historySize }, (_, index) => ({
    id: `history-${index}`, role: index % 2 ? "user" : "assistant",
    content: `${index}: ${plain}\n${product}\n${"這是用於離線效能測試的已完成訊息。".repeat(20)}`,
  }));
  const replayStorage = memoryStorage();
  const gate = new ChatHistoryPersistence();
  const originalStringify = JSON.stringify;
  let serializations = 0;
  JSON.stringify = (...args) => { serializations += 1; return originalStringify(...args); };
  const persist = messages => {
    const committed = messages.filter(item => item.id !== "loading-indicator");
    if (optimized) gate.persist(replayStorage, key, committed, 1000);
    else replayStorage.setItem(key, JSON.stringify({ version: 1, expiresAt: 1000, messages: committed }));
  };
  const start = performance.now();
  try {
    persist(history);
    const user = { id: "new-user", role: "user", content: "請繼續說明" };
    const pending = [...history, user];
    persist([...pending, { id: "loading-indicator", role: "assistant", content: "思考中" }]);
    for (let frame = 0; frame < frames; frame += 1) {
      persist([...pending, { id: "loading-indicator", role: "assistant", content: `回覆 ${frame}` }]);
    }
    persist([...pending, { id: "new-answer", role: "assistant", content: "完整回覆" }]);
  } finally {
    JSON.stringify = originalStringify;
  }
  return { serializations, writes: replayStorage.writes, cpu_ms: +(performance.now() - start).toFixed(3), final: replayStorage.values.get(key) };
}
const results = [];
for (const historySize of [50, 200]) {
  for (const frames of [120, 240]) {
    replay(historySize, frames, false); replay(historySize, frames, true);
    const samples = Array.from({ length: 5 }, () => ({ before: replay(historySize, frames, false), after: replay(historySize, frames, true) }));
    for (const { before, after } of samples) {
      assert.equal(before.serializations, frames + 3);
      assert.equal(before.writes, frames + 3);
      assert.equal(after.serializations, 3);
      assert.equal(after.writes, 3);
      assert.equal(after.final, before.final, "The optimization must preserve the final reloadable history payload");
    }
    const median = kind => samples.map(sample => sample[kind].cpu_ms).sort((a, b) => a - b)[2];
    results.push({ history_messages: historySize, stream_frames: frames, before: { serializations: frames + 3, writes: frames + 3, median_cpu_ms: median("before") }, after: { serializations: 3, writes: 3, median_cpu_ms: median("after") } });
  }
}
console.log(JSON.stringify({ test: "chat-performance", scope: "Node synthetic replay; no browser, model or network calls", cases: results }, null, 2));
console.log("Chat performance passed: committed-history writes, identity isolation, reload payload, error retry, changed content and product links");
