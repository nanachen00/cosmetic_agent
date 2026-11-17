"use client";

import { useEffect, useRef, useState } from "react";
import manifest from "@/public/skin-photo-examples/manifest.json";
import styles from "./PublicPhotoExamples.module.css";

type Context = { localizationBlocked?: boolean };
type Props = { onSelect: (file: File, context?: Context) => Promise<void> | void; onLoadingChange?: (value: boolean) => void; disabled?: boolean };

export default function PublicPhotoExamples({ onSelect, onLoadingChange, disabled = false }: Props) {
  const [loading, setLoading] = useState("");
  const [error, setError] = useState("");
  const pending = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const downloadTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false; controller.current?.abort();
      if (downloadTimer.current !== null) clearTimeout(downloadTimer.current);
    };
  }, []);

  async function select(example: typeof manifest.examples[number]) {
    if (disabled || pending.current) return;
    pending.current = true; setLoading(example.file); setError(""); onLoadingChange?.(true);
    const active = new AbortController(); controller.current = active;
    let timedOut = false;
    const deadline = new Promise<never>((_, reject) => {
      downloadTimer.current = setTimeout(() => {
        timedOut = true; active.abort(); reject(new Error("例圖讀取逾時，請重試。"));
      }, 10_000);
    });
    try {
      const blob = await Promise.race([
        (async () => {
          const response = await fetch(`/skin-photo-examples/${example.file}`, { credentials: "same-origin", signal: active.signal });
          if (!response.ok) throw new Error("例圖暫時無法讀取，請重試。");
          return response.blob();
        })(),
        deadline,
      ]);
      if (downloadTimer.current !== null) clearTimeout(downloadTimer.current);
      downloadTimer.current = null;
      if (!mounted.current || active.signal.aborted) return;
      if (blob.size > 1024 * 1024 || !blob.type.startsWith("image/")) throw new Error("例圖格式不符，請改用其他照片。");
      await onSelect(new File([blob], example.file, { type: blob.type }), { localizationBlocked: example.localizationBlocked });
    } catch (reason) {
      if (mounted.current && (timedOut || !active.signal.aborted)) setError(timedOut ? "例圖讀取逾時，請重試。" : reason instanceof Error ? reason.message : "例圖暫時無法讀取。");
    } finally {
      if (downloadTimer.current !== null) clearTimeout(downloadTimer.current);
      downloadTimer.current = null;
      pending.current = false;
      if (mounted.current) { setLoading(""); onLoadingChange?.(false); }
      if (controller.current === active) controller.current = null;
    }
  }

  return <details className={styles.examples}>
    <summary>選一張公開測試照片 <span>8 張・先預覽，再決定是否分析</span></summary>
    <p>公開例圖只用來測試流程，沒有四類膚況的標準答案。頭像及衍生的低光、模糊、過曝圖可測試拍攝提示；遮眼照片只測膚況流程，不顯示臉部定位。</p>
    <div className={styles.grid}>{manifest.examples.map(example => <article key={example.file}>
      <button type="button" disabled={disabled || Boolean(loading)} onClick={() => void select(example)} aria-label={`選取${example.title}`}>
        <img src={`/skin-photo-examples/${example.file}`} alt="" loading="lazy" width="128" height="128" />
        <strong>{loading === example.file ? "讀取中…" : example.title}</strong>
      </button>
      <small>{example.description}</small>
      <a href={`/skin-photo-examples/${example.file}`} download>下載照片</a>
    </article>)}</div>
    {error && <p role="alert">{error}</p>}
    <p className={styles.credit}>來源：NASA／scikit-image（公眾領域）、Zhenwei Zhai／MEMI-DS v1（CC BY 4.0）、Google Research／SCIN（SCIN Data Use License）。照片經裁切或縮放；衍生壓力圖已標明處理。<a href="/skin-photo-examples/ATTRIBUTION.txt">完整來源與修改</a> · <a href="/skin-photo-examples/SCIN-LICENSE.txt">SCIN 授權</a></p>
  </details>;
}
