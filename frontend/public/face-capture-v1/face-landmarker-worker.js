/* Local-only MediaPipe worker. No camera permission, uploads, or external URLs. */
let landmarker = null;
let initialization = null;
let busy = false;
let disposed = false;
const modelRequest = new AbortController();

async function initialize() {
  if (!initialization) initialization = (async () => {
    importScripts('/face-capture-v1/tasks-vision-1.2.3/vision_bundle.js');
    const files = await Vision.FilesetResolver.forVisionTasks('/face-capture-v1/tasks-vision-1.2.3/wasm');
    const response = await fetch('/face-capture-v1/face_landmarker-float16-v1.task', { signal: modelRequest.signal });
    if (!response.ok) throw new Error('asset_unavailable');
    const model = new Uint8Array(await response.arrayBuffer());
    if (disposed) throw new Error('disposed');
    landmarker = await Vision.FaceLandmarker.createFromOptions(files, {
      baseOptions: { modelAssetBuffer: model, delegate: 'CPU' },
      runningMode: 'IMAGE', numFaces: 2,
      minFaceDetectionConfidence: 0.6, minFacePresenceConfidence: 0.6,
      outputFaceBlendshapes: false, outputFacialTransformationMatrixes: false,
    });
    if (disposed) { landmarker.close(); landmarker = null; throw new Error('disposed'); }
    return landmarker;
  })();
  return initialization;
}

self.onmessage = async ({ data }) => {
  if (data?.type === 'close') {
    disposed = true; modelRequest.abort();
    landmarker?.close(); landmarker = null;
    self.postMessage({ type: 'closed' });
    self.close(); return;
  }
  if (data?.type !== 'frame') return;
  const bitmap = data.bitmap;
  if (disposed || busy) { bitmap?.close(); return; }
  busy = true;
  try {
    const detector = await initialize();
    if (disposed) return;
    const result = detector.detect(bitmap);
    // Only normalized 2D coordinates leave the worker. No depths or fake lesion points.
    const faces = result.faceLandmarks.map(face => face.map(({ x, y }) => ({ x, y })));
    self.postMessage({ type: 'result', id: data.id, faces });
  } catch {
    if (!disposed) self.postMessage({ type: 'unavailable', id: data.id });
  } finally {
    bitmap?.close(); busy = false;
  }
};
