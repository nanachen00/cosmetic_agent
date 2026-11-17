import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

const read = path => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');
const compile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { createPhotoAvailabilityMonitor, classifyPhotoAvailability } = await import('data:text/javascript;base64,' + Buffer.from(compile(read('app/skin-photo/photoAvailability.ts'))).toString('base64'));
const flush = async () => { for (let index = 0; index < 8; index += 1) await Promise.resolve(); };
const response = (status, body) => ({ status, ok: status >= 200 && status < 300, json: async () => body });
function clock() {
  let now = 0, next = 0; const tasks = new Map();
  return {
    tasks,
    setTimer(callback, delay) { const id = ++next; tasks.set(id, { callback, at: now + delay }); return id; },
    clearTimer(id) { tasks.delete(id); },
    async advance(delay) {
      const end = now + delay;
      while (true) {
        const selected = [...tasks.entries()].filter(([, task]) => task.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!selected) break;
        now = selected[1].at; tasks.delete(selected[0]); selected[1].callback(); await flush();
      }
      now = end; await flush();
    },
  };
}
function rig(fetcher) {
  const timer = clock(), changes = []; const state = { visible: true, online: true, calls: 0, signals: [] };
  const monitor = createPhotoAvailabilityMonitor(value => changes.push(value), {
    ...timer, isVisible: () => state.visible, isOnline: () => state.online,
    fetcher: signal => { state.calls += 1; state.signals.push(signal); return fetcher(signal, state.calls); },
  });
  return { monitor, timer, changes, state };
}
assert.equal(classifyPhotoAvailability(200, { ready: true, busy: true }), 'ready');
for (const body of [{ ready: false }, { ready: 'true' }, null]) assert.equal(classifyPhotoAvailability(200, body), 'unavailable');
assert.equal(classifyPhotoAvailability(503, { ready: true }), 'unavailable');
assert.equal(classifyPhotoAvailability(401, null), 'login_required');
assert.equal(classifyPhotoAvailability(403, { ready: true }), 'login_required');
assert.equal(classifyPhotoAvailability(503, { error: 'auth_unavailable' }), 'auth_unavailable');

let resolveFetch;
const single = rig(() => new Promise(resolve => { resolveFetch = resolve; }));
const pending = single.monitor.check();
assert.equal(single.monitor.check(), pending, 'Retry clicks must share a single bounded GET');
assert.equal(single.state.calls, 1);
resolveFetch(response(200, { ready: true })); assert.equal((await pending).status, 'ready');
await single.timer.advance(60000);
assert.equal(single.state.calls, 1, 'A ready visible page must not continuously poll');
assert.equal(single.timer.tasks.size, 0); single.monitor.dispose();

const recovery = rig((_, count) => Promise.resolve(response(count === 1 ? 503 : 200, { ready: count !== 1 })));
assert.equal((await recovery.monitor.check()).status, 'unavailable');
await recovery.timer.advance(29999); assert.equal(recovery.state.calls, 1);
await recovery.timer.advance(1); assert.equal(recovery.state.calls, 2);
assert.equal(recovery.changes.at(-1).status, 'ready');
assert.equal(recovery.timer.tasks.size, 0); recovery.monitor.dispose();

const hidden = rig(() => Promise.resolve(response(503, { ready: false })));
await hidden.monitor.check(); hidden.state.visible = false; hidden.monitor.pause();
await hidden.timer.advance(90000); assert.equal(hidden.state.calls, 1);
assert.equal(hidden.timer.tasks.size, 0);
hidden.state.visible = true; await hidden.monitor.check(); assert.equal(hidden.state.calls, 2);
hidden.monitor.dispose(); assert.equal(hidden.timer.tasks.size, 0);

const stalled = rig(() => new Promise(() => {}));
const stalledResult = stalled.monitor.check(); await stalled.timer.advance(3999); assert.equal(stalled.changes.at(-1).checking, true);
await stalled.timer.advance(1); assert.equal((await stalledResult).status, 'unavailable');
assert.equal(stalled.state.signals[0].aborted, true, 'The hard fetch deadline must abort the request');
stalled.monitor.dispose(); assert.equal(stalled.timer.tasks.size, 0);

const bodyStall = rig(() => Promise.resolve({ status: 200, ok: true, json: () => new Promise(() => {}) }));
const bodyResult = bodyStall.monitor.check(); await flush(); await bodyStall.timer.advance(4000);
assert.equal((await bodyResult).status, 'unavailable', 'Stalled JSON bodies must share the total deadline');
bodyStall.monitor.dispose();

let late;
const cancelled = rig(() => new Promise(resolve => { late = resolve; }));
const cancelledResult = cancelled.monitor.check(); cancelled.state.visible = false; cancelled.monitor.pause();
assert.equal(await cancelledResult, undefined); assert.equal(cancelled.state.signals[0].aborted, true);
const changesAfterPause = cancelled.changes.length;
late(response(200, { ready: true })); await flush(); assert.equal(cancelled.changes.length, changesAfterPause, 'A late response cannot undo hidden-page cancellation');
cancelled.monitor.dispose();

let afterUnmount;
const removed = rig(() => new Promise(resolve => { afterUnmount = resolve; }));
const removedResult = removed.monitor.check(); removed.monitor.dispose();
assert.equal(await removedResult, undefined); const beforeLate = removed.changes.length;
afterUnmount(response(200, { ready: true })); await flush(); assert.equal(removed.changes.length, beforeLate);
assert.equal(removed.timer.tasks.size, 0);

const auth = rig(() => Promise.resolve(response(503, { error: 'auth_unavailable' })));
assert.equal((await auth.monitor.check()).status, 'auth_unavailable'); await auth.timer.advance(90000);
assert.equal(auth.state.calls, 1, 'Anonymous model health checks must not be used to auto-clear an auth-service failure');
auth.monitor.dispose();
const noBodyAuth = rig(() => Promise.resolve({ status: 401, ok: false, json() { assert.fail('Do not require an auth-error JSON body'); } }));
assert.equal((await noBodyAuth.monitor.check()).status, 'login_required'); noBodyAuth.monitor.dispose();
const offline = rig(() => Promise.resolve(response(200, { ready: true })));
offline.state.online = false; assert.equal((await offline.monitor.check()).status, 'unavailable'); assert.equal(offline.state.calls, 0);
offline.state.online = true; assert.equal((await offline.monitor.check()).status, 'ready'); offline.monitor.dispose();

// Run the actual analyze() function with an existing photo and controlled GET/POST
// responses. No browser, camera, pixels or real network requests are used.
const page = read('app/skin-photo/page.tsx');
const syntax = ts.createSourceFile('page.tsx', page, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
let analyzeSource, messagesSource;
function visit(node) {
  if (ts.isFunctionDeclaration(node) && node.name?.text === 'analyze') analyzeSource = node.getText(syntax);
  if (ts.isVariableStatement(node) && node.declarationList.declarations.some(declaration => declaration.name.getText(syntax) === 'messages')) messagesSource = node.getText(syntax);
  ts.forEachChild(node, visit);
}
visit(syntax); assert.ok(analyzeSource && messagesSource);
function pageRig({ canAnalyze = true, health = { status: 'ready', checking: false }, post = response(200, { status: 'needs_confirmation' }) } = {}) {
  const state = { image: 'synthetic-original', preview: 'existing-preview', note: 'keep-note', report: 'previous-report', error: '', posts: [], statuses: [], healthChecks: 0, stopCalls: 0 };
  const timer = clock();
  const context = {
    busy: false, preparing: false, canAnalyze, image: state.image, note: state.note, mounted: { current: true }, request: { current: null }, AbortController, Error, TypeError,
    navigator: { onLine: true }, window: { setTimeout: timer.setTimer, clearTimeout: timer.clearTimer },
    availabilityMonitor: { current: { check: async () => { state.healthChecks += 1; return typeof health === 'function' ? await health() : health; }, setStatus: status => state.statuses.push(status) } },
    stopCamera() { state.stopCalls += 1; }, setBusy() {}, setError(value) { state.error = value; }, setReport(value) { state.report = value; },
    setPreview(value) { state.preview = value; }, setImage(value) { state.image = value; }, setIsSample() {}, setLocalizationBlocked() {},
    fetch: async (url, options) => { state.posts.push({ url, options }); return post; },
  };
  vm.runInNewContext(compile(`${messagesSource}\n${analyzeSource}\nglobalThis.testAnalyze = analyze;`), context);
  return { state, context, timer };
}
const unknown = pageRig({ canAnalyze: false }); await unknown.context.testAnalyze();
assert.equal(unknown.state.posts.length, 0); assert.equal(unknown.state.healthChecks, 0);
for (const sample of [false, true]) {
  const down = pageRig({ health: { status: 'unavailable', checking: false } }); await down.context.testAnalyze(sample);
  assert.equal(down.state.posts.length, 0); assert.equal(down.state.stopCalls, 0);
  assert.equal(down.state.preview, 'existing-preview'); assert.equal(down.state.image, 'synthetic-original');
  assert.equal(down.state.note, 'keep-note'); assert.equal(down.state.report, 'previous-report');
}
const restored = pageRig(); assert.equal(restored.state.posts.length, 0, 'Readiness changes cannot automatically send a photo');
await restored.context.testAnalyze(); assert.equal(restored.state.posts.length, 1);
assert.equal(restored.state.posts[0].options.method, 'POST');
assert.deepEqual(JSON.parse(restored.state.posts[0].options.body), { image_base64: 'synthetic-original', note: 'keep-note' });
assert.deepEqual(restored.state.statuses, ['ready']); assert.equal(restored.timer.tasks.size, 0);

let finishPreflight;
const duplicate = pageRig({ health: () => new Promise(resolve => { finishPreflight = resolve; }) });
const firstAnalysis = duplicate.context.testAnalyze(); await duplicate.context.testAnalyze();
assert.equal(duplicate.state.healthChecks, 1, 'Preflight must synchronously reserve the analysis request');
finishPreflight({ status: 'ready', checking: false }); await firstAnalysis; assert.equal(duplicate.state.posts.length, 1);

const unauth = pageRig({ post: { status: 401, ok: false, json: () => Promise.reject(new SyntaxError('not json')) } });
await unauth.context.testAnalyze(); assert.deepEqual(unauth.state.statuses, ['login_required']); assert.match(unauth.state.error, /登入/);
const authDown = pageRig({ post: response(503, { error: 'auth_unavailable' }) }); await authDown.context.testAnalyze();
assert.deepEqual(authDown.state.statuses, ['auth_unavailable']);
assert.match(page, /disabled=\{busy \|\| preparing \|\| !canAnalyze\}/);
assert.match(page, /window\.removeEventListener\("offline", offline\)/);
assert.match(page, /monitor\.dispose\(\)/);
assert.match(page, /target="_blank" rel="noopener noreferrer">另開分頁登入/);
console.log('Photo availability passed: bounded GET/body, single flight, hidden/offline/unmount cleanup, low-frequency recovery, auth separation, preflight-only POST admission and preserved photo/note/report');
