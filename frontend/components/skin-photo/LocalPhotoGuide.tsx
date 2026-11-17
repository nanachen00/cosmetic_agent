"use client";

import { useEffect, useRef, useState, type ReactNode, type RefObject } from "react";
import { buildFaceRegions, type FaceRegionsResult } from "@/lib/face-regions";
import { analyzePhotoCaptureQuality, type PhotoCaptureQuality } from "@/lib/photo-capture-quality";
import { FaceGuideClient } from "./faceGuideClient";
import styles from "./LocalPhotoGuide.module.css";

type Media = HTMLVideoElement | HTMLImageElement;
type Props = { children: ReactNode; mediaRef: RefObject<Media | null>; sourceKey: string; kind: "video" | "image"; mirrored?: boolean; localizationBlocked?: boolean };

export default function LocalPhotoGuide({ children, mediaRef, sourceKey, kind, mirrored = false, localizationBlocked = false }: Props) {
  const [enabledFor, setEnabledFor] = useState<string | null>(null);
  const enabled = enabledFor === sourceKey && !localizationBlocked;
  const [quality, setQuality] = useState<PhotoCaptureQuality | null>(null);
  const [geometry, setGeometry] = useState<FaceRegionsResult | null>(null);
  const [dimensions, setDimensions] = useState({ width: 1, height: 1 });
  const [status, setStatus] = useState<"loading" | "ready" | "unavailable" | "disabled">("loading");
  const qualityKey = useRef("");

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let client: FaceGuideClient | null = null;
    let workerFailed = false;
    const canvas = document.createElement("canvas");
    const context = canvas.getContext("2d", { willReadFrequently: true });
    setGeometry(null); setQuality(null); qualityKey.current = "";
    setStatus(enabled ? "loading" : "disabled");
    if (enabled && typeof Worker !== "undefined" && typeof OffscreenCanvas !== "undefined" && typeof createImageBitmap === "function") {
      try { client = new FaceGuideClient(new Worker("/face-capture-v1/face-landmarker-worker.js")); }
      catch { workerFailed = true; setStatus("unavailable"); }
    } else if (enabled) { workerFailed = true; setStatus("unavailable"); }

    const schedule = () => { if (!disposed && kind === "video") timer = setTimeout(sample, 400); };
    async function sample() {
      if (disposed || !context) return;
      const media = mediaRef.current;
      const width = media instanceof HTMLVideoElement ? media.videoWidth : media?.naturalWidth;
      const height = media instanceof HTMLVideoElement ? media.videoHeight : media?.naturalHeight;
      if (!media || !width || !height || (media instanceof HTMLVideoElement && media.readyState < 2)) {
        if (!disposed) timer = setTimeout(sample, 400);
        return;
      }
      if (document.hidden) { timer = setTimeout(sample, 400); return; }
      const ratio = Math.min(1, 320 / Math.max(width, height));
      canvas.width = Math.max(3, Math.round(width * ratio)); canvas.height = Math.max(3, Math.round(height * ratio));
      try {
        context.drawImage(media, 0, 0, canvas.width, canvas.height);
        const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
        const initial = analyzePhotoCaptureQuality(pixels);
        const publishQuality = (next: PhotoCaptureQuality) => {
          const key = JSON.stringify([next.valid, next.exposure, next.blurHint, next.region, next.hints]);
          if (!disposed && key !== qualityKey.current) { qualityKey.current = key; setQuality(next); }
        };
        if (!qualityKey.current) publishQuality(initial);
        let face: FaceRegionsResult | null = null;
        if (client && !workerFailed) {
          const bitmap = await createImageBitmap(canvas);
          if (disposed) { bitmap.close(); return; }
          try {
            const result = await client.analyze(bitmap);
            if (disposed) return;
            face = buildFaceRegions(result.faces[0], { faceCount: result.faces.length, imageWidth: width, imageHeight: height });
            setStatus("ready");
          } catch {
            if (disposed) return;
            workerFailed = true; client.dispose(); setStatus("unavailable");
          }
        }
        const next = face?.reliable ? analyzePhotoCaptureQuality(pixels, face.facePolygon) : initial;
        if (disposed) return;
        // Avoid feeding per-frame pixels/landmarks into the parent page's state.
        publishQuality(next);
        setDimensions(current => current.width === width && current.height === height ? current : { width, height });
        setGeometry(face);
      } catch {
        if (!disposed) { setStatus("unavailable"); setGeometry(null); }
      }
      schedule();
    }
    timer = setTimeout(sample, 0);
    return () => { disposed = true; if (timer !== null) clearTimeout(timer); client?.dispose(); canvas.width = 0; canvas.height = 0; };
  }, [sourceKey, kind, mediaRef, enabled]);

  const hints = [...(quality?.hints || []), ...(enabled && geometry ? geometry.hints : [])];
  const summary = !quality ? "正在檢查畫面…" : quality.hints.length ? "可以再調整拍攝條件" : "未發現明顯警訊，仍請確認部位看得清楚";
  return <>
    <div className={styles.stage}>
      {children}
      {enabled && geometry?.reliable && <svg className={`${styles.overlay} ${mirrored ? styles.mirrored : ""}`} viewBox={`0 0 ${dimensions.width} ${dimensions.height}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label="依這張臉的定位點繪製的粗略部位，並非病灶位置">
        {geometry.regions.map(region => <polygon key={region.id} points={region.polygon.map(point => `${point.x * dimensions.width},${point.y * dimensions.height}`).join(" ")}><title>{region.label}（粗略部位）</title></polygon>)}
      </svg>}
    </div>
    <div className={styles.guide}>
      <label className={styles.toggle}><input type="checkbox" checked={enabled} disabled={localizationBlocked} onChange={event => setEnabledFor(event.target.checked ? sourceKey : null)} />眼鼻輪廓清楚可見，顯示部位參考</label>
      {localizationBlocked ? <p>此公開例圖有遮擋，部位定位不適用；仍可分析可見皮膚。</p> : <p>只在眼鼻輪廓完整、沒有遮擋時啟用。此功能不能自動確認遮擋，近照不必開啟定位。</p>}
      <p className={styles.summary} role="status">{summary}</p>
      <p>沒有提示不代表光線充足；看不清時可增加柔和光源後重拍。</p>
      {hints.length > 0 && <ul>{Array.from(new Map(hints.map(hint => [hint.code, hint])).values()).map(hint => <li key={hint.code}>{hint.message}</li>)}</ul>}
      {enabled && status === "loading" && <p>正在載入本機部位定位…</p>}
      {enabled && status === "unavailable" && <p>目前無法提供個人部位定位；仍可使用清楚的近照分析。</p>}
      {enabled && geometry?.reliable && <div className={styles.legend}>{geometry.regions.map(region => <span key={region.id}>{region.label}</span>)}</div>}
      <p className={styles.limit}>光線／清晰度提示與2D部位定位只在此瀏覽器處理，不會自動上傳。區域僅供拍攝參考，不能測量真實凹陷深度或標定痘點。</p>
    </div>
  </>;
}
