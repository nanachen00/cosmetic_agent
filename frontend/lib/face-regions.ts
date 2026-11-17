/** Coarse 2D display regions, never lesion locations or metric depth.
 * Contour semantics verified against MediaPipe's official connections:
 * https://github.com/google-ai-edge/mediapipe/blob/master/mediapipe/tasks/web/vision/face_landmarker/face_landmarks_connections.ts
 * https://github.com/google-ai-edge/mediapipe/blob/master/mediapipe/python/solutions/face_mesh_connections.py
 * The additional area hulls are our conservative UI approximation, not an
 * official anatomical segmentation or validated pixel-accuracy claim.
 */
export type NormalizedPoint = { x: number; y: number };
export type NormalizedLandmark = NormalizedPoint & { z?: number; visibility?: number; presence?: number };
export type CaptureHint = { code: string; message: string };
export type FaceRegionId = 'forehead' | 'left_cheek' | 'right_cheek' | 'nose' | 'chin' | 'left_eye' | 'right_eye';
export type ModelFaceRegion = 'forehead' | 'cheek' | 'nose' | 'chin' | 'eye_area';
export type FaceRegion = {
  id: FaceRegionId; modelRegion: ModelFaceRegion; polygon: NormalizedPoint[];
  label: string; kind: 'area' | 'outline';
};
export type FaceRegionsResult = {
  /** Geometry passed display heuristics; this is not calibrated confidence. */
  reliable: boolean; regions: FaceRegion[]; facePolygon: NormalizedPoint[];
  faceOutline: NormalizedPoint[]; rollDegrees: number | null;
  pose: { yawProxy: number | null; pitchProxy: number | null };
  reasons: string[]; hints: CaptureHint[]; coordinateSpace: 'normalized-original-image';
};

export const FACE_OVAL_INDICES = [10,338,297,332,284,251,389,356,454,323,361,288,397,365,379,378,400,377,152,148,176,149,150,136,172,58,132,93,234,127,162,21,54,103,67,109] as const;
const LEFT_EYE = [263,249,390,373,374,380,381,382,362,398,384,385,386,387,388,466];
const RIGHT_EYE = [33,7,163,144,145,153,154,155,133,173,157,158,159,160,161,246];
const FOREHEAD = [10,338,297,332,284,251,389,356,127,162,21,54,103,67,109,70,63,105,66,107,336,296,334,293,300];
const LEFT_CHEEK = [263,373,374,380,381,382,362,327,291,288,361,323,454];
const RIGHT_CHEEK = [33,144,145,153,154,155,133,98,61,58,132,93,234];
const NOSE = [168,6,197,195,5,4,1,19,94,2,98,97,64,48,115,220,45,275,440,344,278,294,327,326];
const CHIN = [17,84,314,148,152,377,176,400];

const REASONS: Record<string, string> = {
  no_face: '未取得完整臉部輪廓；皮膚近照仍可送出分析。',
  multiple_faces: '畫面有多張臉，請讓要拍攝的人單獨入鏡。',
  insufficient_landmarks: '暫時無法穩定取得臉部輪廓。',
  invalid_dimensions: '暫時無法對齊影像尺寸。',
  face_out_of_frame: '臉部輪廓超出畫面，請稍微退後。',
  degenerate_geometry: '輪廓暫時不穩定，請面向鏡頭並保持穩定。',
  small_face: '臉部在畫面中較小，請在清楚對焦下稍微靠近。',
  pose_too_oblique: '請稍微轉向鏡頭；目前先不顯示臉部區域。',
  inconsistent_regions: '臉部區域暫時無法可靠對齊。',
};

export function polygonArea(points: readonly NormalizedPoint[]): number {
  let twice = 0;
  for (let i = 0; i < points.length; i++) {
    const a = points[i], b = points[(i + 1) % points.length];
    twice += a.x * b.y - b.x * a.y;
  }
  return Math.abs(twice) / 2;
}

function cross(a: NormalizedPoint, b: NormalizedPoint, c: NormalizedPoint): number {
  return (b.x-a.x)*(c.y-a.y)-(b.y-a.y)*(c.x-a.x);
}

function hull(points: NormalizedPoint[]): NormalizedPoint[] {
  const sorted = points.slice().sort((a,b) => a.x-b.x || a.y-b.y);
  const unique = sorted.filter((p,i) => !i || p.x !== sorted[i-1].x || p.y !== sorted[i-1].y);
  if (unique.length < 3) return [];
  const lower: NormalizedPoint[] = [], upper: NormalizedPoint[] = [];
  for (const p of unique) { while (lower.length >= 2 && cross(lower.at(-2)!, lower.at(-1)!, p) <= 0) lower.pop(); lower.push(p); }
  for (const p of unique.slice().reverse()) { while (upper.length >= 2 && cross(upper.at(-2)!, upper.at(-1)!, p) <= 0) upper.pop(); upper.push(p); }
  return lower.slice(0,-1).concat(upper.slice(0,-1));
}

/** Boundary points count as inside. No clipping or invented boundary is used. */
export function pointInPolygon(point: NormalizedPoint, polygon: readonly NormalizedPoint[]): boolean {
  let inside = false;
  for (let i = 0, j = polygon.length-1; i < polygon.length; j=i++) {
    const a = polygon[j], b = polygon[i];
    if (Math.abs(cross(a,b,point)) < 1e-9 && point.x >= Math.min(a.x,b.x)-1e-9 && point.x <= Math.max(a.x,b.x)+1e-9 && point.y >= Math.min(a.y,b.y)-1e-9 && point.y <= Math.max(a.y,b.y)+1e-9) return true;
    if ((a.y > point.y) !== (b.y > point.y) && point.x < (b.x-a.x)*(point.y-a.y)/(b.y-a.y)+a.x) inside = !inside;
  }
  return inside;
}

export function buildFaceRegions(
  landmarks: readonly NormalizedLandmark[] | null | undefined,
  options: { faceCount?: number; imageWidth?: number; imageHeight?: number } = {},
): FaceRegionsResult {
  const result: FaceRegionsResult = { reliable:false, regions:[], facePolygon:[], faceOutline:[], rollDegrees:null,
    pose:{yawProxy:null,pitchProxy:null}, reasons:[], hints:[], coordinateSpace:'normalized-original-image' };
  const fail = (reason: string) => { result.reasons.push(reason); result.hints.push({code:reason,message:REASONS[reason]}); return result; };
  const count = options.faceCount ?? (landmarks?.length ? 1 : 0);
  if (!count || !landmarks?.length) return fail('no_face');
  if (count !== 1) return fail('multiple_faces');
  if (landmarks.length < 468 || landmarks.length > 512) return fail('insufficient_landmarks');
  const width = options.imageWidth ?? 1, height = options.imageHeight ?? 1;
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0) return fail('invalid_dimensions');
  // z is deliberately ignored: its scale is not measured physical depth.
  for (let i = 0; i < 468; i++) {
    const p = landmarks[i];
    if (!p || !Number.isFinite(p.x) || !Number.isFinite(p.y)) return fail('insufficient_landmarks');
    if (p.x < 0 || p.x > 1 || p.y < 0 || p.y > 1) return fail('face_out_of_frame');
  }
  const pick = (indices: readonly number[]) => indices.map(i => ({x:landmarks[i].x,y:landmarks[i].y}));
  const oval = pick(FACE_OVAL_INDICES), area = polygonArea(oval);
  if (area < 1e-6 || polygonArea(hull(oval)) > area * 1.15) return fail('degenerate_geometry');
  if (area < .015) return fail('small_face');
  const pixel = (i:number) => ({x:landmarks[i].x*width,y:landmarks[i].y*height});
  const a = pixel(33), b = pixel(263), top = pixel(10), bottom = pixel(152);
  const span = Math.hypot(b.x-a.x,b.y-a.y);
  const faceHeight = Math.hypot(bottom.x-top.x,bottom.y-top.y);
  if (span <= faceHeight * .05 || faceHeight <= 1e-8) return fail('degenerate_geometry');
  const ex = {x:(b.x-a.x)/span,y:(b.y-a.y)/span};
  let down = {x:-ex.y,y:ex.x};
  if ((bottom.x-top.x)*down.x+(bottom.y-top.y)*down.y < 0) down = {x:-down.x,y:-down.y};
  const eyeMid = {x:(a.x+b.x)/2,y:(a.y+b.y)/2};
  const nose = pixel(1), mouthA = pixel(61), mouthB = pixel(291);
  const project = (p:NormalizedPoint) => (p.x-eyeMid.x)*down.x+(p.y-eyeMid.y)*down.y;
  const chinDistance = project(bottom), noseDistance = project(nose);
  const mouthDistance = (project(mouthA)+project(mouthB))/2;
  if (chinDistance <= 0 || project(top) >= 0 || noseDistance <= 0 || mouthDistance <= noseDistance || mouthDistance >= chinDistance) return fail('degenerate_geometry');
  let roll = Math.atan2(ex.y,ex.x)*180/Math.PI;
  if (roll > 90) roll -= 180; if (roll < -90) roll += 180;
  const yaw = ((nose.x-eyeMid.x)*ex.x+(nose.y-eyeMid.y)*ex.y)/span;
  const pitch = noseDistance/chinDistance;
  result.rollDegrees=roll; result.pose={yawProxy:yaw,pitchProxy:pitch};
  if (Math.abs(roll) > 45 || Math.abs(yaw) > .35 || pitch < .10 || pitch > .65) return fail('pose_too_oblique');
  const specifications: [FaceRegionId,ModelFaceRegion,string,number[], 'area'|'outline'][] = [
    ['forehead','forehead','額頭',FOREHEAD,'area'],
    ['left_cheek','cheek','左側臉頰',LEFT_CHEEK,'area'], ['right_cheek','cheek','右側臉頰',RIGHT_CHEEK,'area'],
    ['nose','nose','鼻部',NOSE,'area'], ['chin','chin','下巴',CHIN,'area'],
    ['left_eye','eye_area','左眼輪廓',LEFT_EYE,'outline'], ['right_eye','eye_area','右眼輪廓',RIGHT_EYE,'outline'],
  ];
  for (const [id,modelRegion,label,indices,kind] of specifications) {
    const points = pick(indices), polygon = kind === 'area' ? hull(points) : points;
    if (polygonArea(polygon) < area * (kind === 'outline' ? .00001 : .003)) {
      if (kind === 'outline') continue; // Closed eyes need no invented eye area.
      result.regions=[]; return fail('inconsistent_regions');
    }
    if (polygon.some(p => !pointInPolygon(p,oval))) { result.regions=[]; return fail('inconsistent_regions'); }
    result.regions.push({id,modelRegion,label,polygon,kind});
  }
  result.reliable=true; result.facePolygon=oval; result.faceOutline=oval.map(p => ({...p}));
  return result;
}

/** The model names a coarse region only; never manufacture a lesion point. */
export function regionsForObservation(region: string, result: FaceRegionsResult): FaceRegion[] {
  if (!result.reliable || !['forehead','cheek','nose','chin','eye_area'].includes(region)) return [];
  return result.regions.filter(item => item.modelRegion === region);
}
