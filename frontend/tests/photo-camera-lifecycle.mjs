import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import ts from 'typescript';

const read = path => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');
const compiled = ts.transpileModule(read('components/skin-photo/faceGuideClient.ts'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { FaceGuideClient } = await import('data:text/javascript;base64,' + Buffer.from(compiled).toString('base64'));
class FakeWorker {
  onmessage = null; onerror = null; messages = []; terminations = 0;
  postMessage(value, transfer) { this.messages.push({ value, transfer }); }
  terminate() { this.terminations += 1; }
  receive(data) { this.onmessage?.({ data }); }
}
const bitmap = () => ({ closed: 0, close() { this.closed += 1; } });
const worker = new FakeWorker(), client = new FaceGuideClient(worker, 1000, 5);
const first = bitmap(), second = bitmap();
const reading = client.analyze(first);
await assert.rejects(client.analyze(second), /frame_not_accepted/);
assert.equal(second.closed, 1, 'Untransferred duplicate frames must be released instead of queued');
assert.equal(worker.messages.length, 1);
assert.equal(worker.messages[0].transfer[0], first, 'Transfer one bitmap; do not serialize photo data into strings');
worker.receive({ type: 'result', id: 999, faces: [] });
worker.receive({ type: 'result', id: 1, faces: [[{ x: .2, y: .4 }]] });
assert.deepEqual(await reading, { faces: [[{ x: .2, y: .4 }]] });
client.dispose(); client.dispose(); worker.receive({ type: 'closed' });
assert.equal(worker.messages.filter(message => message.value.type === 'close').length, 1);
assert.equal(worker.terminations, 1);
const afterClose = bitmap(); await assert.rejects(client.analyze(afterClose)); assert.equal(afterClose.closed, 1);

const pendingWorker = new FakeWorker(), pendingClient = new FaceGuideClient(pendingWorker, 1000, 5);
const pending = pendingClient.analyze(bitmap()); const cancelled = assert.rejects(pending, /guide_closed/);
pendingClient.dispose(); await cancelled;
pendingWorker.receive({ type: 'result', id: 1, faces: [] });
await new Promise(resolve => setTimeout(resolve, 15));
assert.equal(pendingWorker.terminations, 1, 'Unresponsive shutdown must terminate the worker');

const stalledWorker = new FakeWorker(), stalledClient = new FaceGuideClient(stalledWorker, 10, 5);
await assert.rejects(stalledClient.analyze(bitmap()), /guide_closed/);
await new Promise(resolve => setTimeout(resolve, 15));
assert.equal(stalledWorker.terminations, 1);

const invalidWorker = new FakeWorker(), invalidClient = new FaceGuideClient(invalidWorker, 1000, 5);
const invalid = invalidClient.analyze(bitmap()); invalidWorker.receive({ type: 'result', id: 1, faces: [[{ x: NaN, y: .2 }]] });
await assert.rejects(invalid, /landmarks_unavailable/); invalidClient.dispose(); invalidWorker.receive({ type: 'closed' });

const errorWorker = new FakeWorker(), errorClient = new FaceGuideClient(errorWorker, 1000, 5);
const errored = errorClient.analyze(bitmap()); const rejected = assert.rejects(errored, /guide_closed/);
errorWorker.onerror({}); await rejected; errorWorker.receive({ type: 'closed' });
assert.equal(errorWorker.terminations, 1);

const script = read('public/face-capture-v1/face-landmarker-worker.js');
const emitted = [], requests = []; let initialized = 0, modelClosed = 0;
const scope = { postMessage(message) { emitted.push(message); }, close() {} };
const sandbox = { self: scope, AbortController, Uint8Array,
  importScripts(url) { assert.equal(url, '/face-capture-v1/tasks-vision-1.2.3/vision_bundle.js'); },
  fetch: async (url, options) => { requests.push({ url, options }); return { ok: true, arrayBuffer: async () => new ArrayBuffer(1) }; },
  Vision: { FilesetResolver: { forVisionTasks: async path => { assert.equal(path, '/face-capture-v1/tasks-vision-1.2.3/wasm'); return {}; } },
    FaceLandmarker: { createFromOptions: async (_, options) => { initialized += 1; assert.equal(options.numFaces, 2); assert.equal(options.runningMode, 'IMAGE');
      return { detect: () => ({ faceLandmarks: [[{ x: .2, y: .3, z: 70 }]] }), close: () => { modelClosed += 1; } }; } } },
};
vm.runInNewContext(script, sandbox);
const a = bitmap(), b = bitmap();
await scope.onmessage({ data: { type: 'frame', id: 1, bitmap: a } });
await scope.onmessage({ data: { type: 'frame', id: 2, bitmap: b } });
assert.equal(initialized, 1, 'Load one detector for the session instead of per frame');
assert.equal(a.closed, 1); assert.equal(b.closed, 1);
assert.deepEqual(JSON.parse(JSON.stringify(emitted[0].faces)), [[{ x: .2, y: .3 }]], 'Only 2D landmarks may leave the worker');
assert.equal(requests.length, 1);
assert.equal(requests[0].url, '/face-capture-v1/face_landmarker-float16-v1.task');
await scope.onmessage({ data: { type: 'close' } });
assert.equal(modelClosed, 1); assert.equal(requests[0].options.signal.aborted, true);

const provenance = JSON.parse(read('public/face-capture-v1/provenance.json'));
assert.equal(provenance.models_downloaded, 1);
assert.equal(provenance.package.version, JSON.parse(read('package.json')).dependencies['@mediapipe/tasks-vision']);
const { FilesetResolver } = createRequire(import.meta.url)('@mediapipe/tasks-vision');
const resolved = await FilesetResolver.forVisionTasks('/face-capture-v1/tasks-vision-1.2.3/wasm');
assert.equal(resolved.wasmLoaderPath, '/face-capture-v1/tasks-vision-1.2.3/wasm/vision_wasm_internal.js');
assert.equal(resolved.wasmBinaryPath, '/face-capture-v1/tasks-vision-1.2.3/wasm/vision_wasm_internal.wasm');
for (const asset of provenance.assets) {
  const bytes = readFileSync(new URL(`../public/${asset.path}`, import.meta.url));
  assert.equal(bytes.length, asset.bytes); assert.equal(createHash('sha256').update(bytes).digest('hex'), asset.local_sha256);
}
const page = read('app/skin-photo/page.tsx'), guide = read('components/skin-photo/LocalPhotoGuide.tsx');
const syntax = ts.createSourceFile('page.tsx', page, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const cameraFunctions = [];
const visit = node => {
  if (ts.isFunctionDeclaration(node) && ['openCamera', 'stopCamera', 'chooseFile', 'takePhoto', 'clearPhoto'].includes(node.name?.text)) cameraFunctions.push(node.getText(syntax));
  ts.forEachChild(node, visit);
};
visit(syntax); assert.equal(cameraFunctions.length, 5);
let resolvePermission, cameraCalls = 0, stoppedTracks = 0;
const cameraState = { pending: false, open: false };
const cameraSandbox = {
  window: { isSecureContext: true }, Error, busy: false, preparing: false,
  navigator: { mediaDevices: { getUserMedia: options => { cameraCalls += 1; assert.equal(options.audio, false); return new Promise(resolve => { resolvePermission = resolve; }); } } },
  attempt: { current: 0 }, mounted: { current: true }, stream: { current: null }, video: { current: { srcObject: null } },
  setError() {}, setCameraMessage() {}, setFillLight() {},
  setCameraPending(value) { cameraState.pending = value; }, setCameraOpen(value) { cameraState.open = value; },
};
const cameraJs = ts.transpileModule(cameraFunctions.join('\n') + '\nglobalThis.testOpen = openCamera; globalThis.testStop = stopCamera; globalThis.testChoose = chooseFile; globalThis.testCapture = takePhoto; globalThis.testClear = clearPhoto;', { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
vm.runInNewContext(cameraJs, cameraSandbox);
assert.equal(cameraCalls, 0, 'Defining/rendering camera controls must never request camera permission');
cameraSandbox.preparing = true;
await cameraSandbox.testOpen();
await cameraSandbox.testChoose({ target: { files: [{}], value: 'selected' } });
await cameraSandbox.testCapture(); cameraSandbox.testClear();
assert.equal(cameraCalls, 0, 'Gallery prefetch must block camera/file/capture/clear before accessing a private source');
cameraSandbox.preparing = false;
const permission = cameraSandbox.testOpen(); assert.equal(cameraCalls, 1); assert.equal(cameraState.pending, true);
cameraSandbox.testStop(); resolvePermission({ getTracks: () => [{ stop() { stoppedTracks += 1; } }] }); await permission;
assert.equal(stoppedTracks, 1, 'A permission grant that arrives after cancel must immediately release its tracks');
assert.equal(cameraSandbox.stream.current, null); assert.equal(cameraState.open, false);
const active = cameraSandbox.testOpen(); resolvePermission({ getTracks: () => [{ stop() { stoppedTracks += 1; } }] }); await active;
assert.equal(cameraState.open, true); cameraSandbox.testStop();
assert.equal(stoppedTracks, 2); assert.equal(cameraSandbox.video.current.srcObject, null);
assert.match(guide, /setTimeout\(sample, 400\)/, 'Preserve bounded capture sampling');
assert.match(guide, /enabledFor === sourceKey && !localizationBlocked/, 'Known occluded samples must not enable inferred overlays');
assert.match(guide, /checked=\{enabled\} disabled=\{localizationBlocked\}/);
assert.match(page, /const preparing = preparingPhoto \|\| exampleLoading;/);
assert.match(page, /onLoadingChange=\{setExampleLoading\}/);
assert.match(guide, /imageWidth: width, imageHeight: height/, 'Keep normalized geometry bound to the original displayed aspect ratio');
assert.doesNotMatch(page, /context\.filter|\.filter\s*=|brightness\(/, 'Capture pixels must not be enhanced by a display-light control');
assert.match(page, /context\.translate\(canvas\.width, 0\); context\.scale\(-1, 1\); context\.drawImage\(current, 0, 0\);/, 'Preserve the existing mirrored camera capture pipeline');
console.log('Photo camera lifecycle passed: bounded transfer/admission, cancellation, timeout/error cleanup, one local model, 2D-only output and verified static assets');
