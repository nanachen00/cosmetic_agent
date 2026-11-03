"use client";

import Link from "next/link";
import { useEffect, useRef, useState, type ChangeEvent } from "react";
import styles from "./SkinPhoto.module.css";
import { groupFindings, uniqueStatements } from "./reportSummary";

type Finding = { tag: string; region: string; evidence: string };
type Report = {
  status: string; input_source?: string; inference_seconds?: number;
  observation?: { image_scope: string; observations: Finding[]; limitations: string[]; follow_up_question: string };
};
const labels: Record<string, string> = { acne_like_bumps: "痘痘樣突起", redness: "泛紅", dark_marks: "深色痕跡", flaking: "脫屑" };
const regions: Record<string, string> = { forehead: "額頭", cheek: "臉頰", nose: "鼻子", chin: "下巴", eye_area: "眼周", unknown: "部位待確認" };
const messages: Record<string, string> = {
  login_required: "請先登入 BLINK AI，再分析照片。", auth_unavailable: "暫時無法確認登入，請稍後再試。",
  rate_limited: "這一分鐘已分析多次，請稍候再試。", busy: "目前正在處理另一張照片，請稍後再試。",
  model_starting: "照片分析正在準備中，請稍後再試。", image_too_large: "照片太大，請選擇較小的檔案。",
  invalid_image: "無法讀取這張照片，請改用 JPG、PNG 或 WebP。",
  inference_unavailable: "照片分析暫時無法使用，你可以保留照片後再試。",
};

function CameraIcon() {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><path d="M8 6l1.5-2h5L16 6h3a2 2 0 012 2v11H3V8a2 2 0 012-2h3Z"/><circle cx="12" cy="12" r="4"/></svg>;
}

async function preparePhoto(file: File): Promise<{ image: string; preview: string }> {
  if (!file.type.startsWith("image/") || file.size > 12 * 1024 * 1024) throw new Error("請選擇 12 MB 以下的圖片。");
  const source = URL.createObjectURL(file);
  try {
    const image = new Image(); image.src = source;
    await image.decode();
    if (Math.min(image.naturalWidth, image.naturalHeight) < 224) throw new Error("照片太小，請選擇至少 224 × 224 像素的照片。");
    if (image.naturalWidth * image.naturalHeight > 25_000_000) throw new Error("照片解析度過高，請先選擇較小的照片。");
    const ratio = Math.min(1, 1280 / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(image.naturalWidth * ratio); canvas.height = Math.round(image.naturalHeight * ratio);
    const context = canvas.getContext("2d");
    if (!context) throw new Error("目前瀏覽器無法處理照片。");
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    const preview = canvas.toDataURL("image/jpeg", .9);
    const encoded = preview.split(",")[1];
    if (!encoded || encoded.length > 4 * 1024 * 1024) throw new Error("照片仍太大，請改用較小的照片。");
    return { image: encoded, preview };
  } catch (error) {
    if (error instanceof Error && error.name !== "EncodingError") throw error;
    throw new Error("瀏覽器無法讀取這張照片，請改用 JPG 或 PNG。");
  } finally { URL.revokeObjectURL(source); }
}

export default function SkinPhotoPage() {
  const fileInput = useRef<HTMLInputElement>(null);
  const nativeCamera = useRef<HTMLInputElement>(null);
  const video = useRef<HTMLVideoElement>(null);
  const stream = useRef<MediaStream | null>(null);
  const attempt = useRef(0);
  const request = useRef<AbortController | null>(null);
  const mounted = useRef(true);
  const [cameraOpen, setCameraOpen] = useState(false);
  const [cameraPending, setCameraPending] = useState(false);
  const [image, setImage] = useState("");
  const [preview, setPreview] = useState("");
  const [isSample, setIsSample] = useState(false);
  const [busy, setBusy] = useState(false);
  const [preparing, setPreparing] = useState(false);
  const [note, setNote] = useState("");
  const [error, setError] = useState("");
  const [cameraMessage, setCameraMessage] = useState("");
  const [report, setReport] = useState<Report | null>(null);
  const [ready, setReady] = useState<boolean | null>(null);

  function stopCamera() {
    attempt.current += 1;
    stream.current?.getTracks().forEach(track => track.stop()); stream.current = null;
    if (video.current) video.current.srcObject = null;
    setCameraOpen(false); setCameraPending(false);
  }

  useEffect(() => {
    mounted.current = true;
    const controller = new AbortController();
    void fetch("/api/skin-photo", { cache: "no-store", signal: controller.signal })
      .then(response => response.json()).then(result => { if (mounted.current) setReady(Boolean(result.ready)); })
      .catch(() => { if (mounted.current) setReady(false); });
    const hide = () => { if (document.hidden) stopCamera(); };
    document.addEventListener("visibilitychange", hide);
    return () => {
      mounted.current = false; controller.abort(); request.current?.abort(); attempt.current += 1;
      stream.current?.getTracks().forEach(track => track.stop()); stream.current = null;
      document.removeEventListener("visibilitychange", hide);
    };
  }, []);

  useEffect(() => {
    if (!cameraOpen || !video.current || !stream.current) return;
    video.current.srcObject = stream.current;
    void video.current.play().catch(() => { stopCamera(); setCameraMessage("無法顯示相機預覽，請改用選擇照片。"); });
  }, [cameraOpen]);

  async function openCamera() {
    setError(""); setCameraMessage("");
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      setCameraMessage("這個網址無法開啟網頁相機。請使用 HTTPS 或這台電腦的 localhost 網址；也可以直接選擇照片。"); return;
    }
    stopCamera(); const token = ++attempt.current; setCameraPending(true);
    setCameraMessage("請在瀏覽器提示中選擇允許相機。只會開啟預覽，按下分析前不會傳送照片。");
    try {
      const media = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "user", width: { ideal: 1024 }, height: { ideal: 1024 } }, audio: false });
      if (!mounted.current || token !== attempt.current) { media.getTracks().forEach(track => track.stop()); return; }
      stream.current = media; setCameraOpen(true); setCameraPending(false); setCameraMessage("");
    } catch (reason) {
      if (!mounted.current || token !== attempt.current) return;
      setCameraPending(false);
      const name = reason instanceof Error ? reason.name : "";
      setCameraMessage(name === "NotAllowedError" ? "尚未取得相機權限。請在網址列的網站設定允許相機；若系統也封鎖瀏覽器，需在系統相機設定開啟。你仍可選擇照片。" :
        name === "NotFoundError" ? "目前沒有找到相機，請改用選擇照片。" :
        name === "NotReadableError" ? "相機可能正被其他程式使用，請關閉後重試，或改用選擇照片。" : "無法開啟相機，請重試或選擇照片。");
    }
  }

  async function chooseFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0]; event.target.value = "";
    if (!file) return;
    stopCamera(); setError(""); setReport(null); setPreparing(true);
    try {
      const result = await preparePhoto(file);
      if (mounted.current) { setImage(result.image); setPreview(result.preview); setIsSample(false); }
    } catch (reason) { setError(reason instanceof Error ? reason.message : "無法讀取照片。"); }
    finally { if (mounted.current) setPreparing(false); }
  }

  async function takePhoto() {
    const current = video.current;
    if (!current?.videoWidth) { setCameraMessage("相機還在準備，請稍候。"); return; }
    const canvas = document.createElement("canvas"); canvas.width = current.videoWidth; canvas.height = current.videoHeight;
    const context = canvas.getContext("2d"); if (!context) return;
    context.translate(canvas.width, 0); context.scale(-1, 1); context.drawImage(current, 0, 0);
    const blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, "image/jpeg", .92));
    stopCamera();
    if (!blob) { setError("拍攝未完成，請再試一次。"); return; }
    try { const photo = await preparePhoto(new File([blob], "camera.jpg", { type: "image/jpeg" })); setImage(photo.image); setPreview(photo.preview); setIsSample(false); setReport(null); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "無法處理照片。"); }
  }

  async function analyze(sample = false) {
    if (busy || (!sample && !image)) return;
    stopCamera(); setBusy(true); setError(""); setReport(null);
    if (sample) { setPreview("/skin-photo-demo.jpg"); setImage(""); setIsSample(true); }
    const controller = new AbortController(); request.current = controller;
    const timer = window.setTimeout(() => controller.abort(), 95000);
    try {
      const response = await fetch("/api/skin-photo", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(sample ? { sample: true } : { image_base64: image, note }), signal: controller.signal });
      const result = await response.json();
      if (!response.ok) throw new Error(messages[result.error] || "這次分析未完成，請稍後再試。");
      if (mounted.current) { setReport(result as Report); setReady(true); }
    } catch (reason) {
      if (mounted.current) setError(reason instanceof Error && reason.name === "AbortError" ? "分析等待較久，已停止等待；請稍後再試。" : reason instanceof Error ? reason.message : "這次分析未完成。");
    } finally { window.clearTimeout(timer); if (mounted.current) setBusy(false); request.current = null; }
  }

  function clearPhoto() { stopCamera(); setImage(""); setPreview(""); setReport(null); setError(""); setIsSample(false); }

  const findingGroups = groupFindings(report?.observation?.observations || []);
  const limitations = uniqueStatements(report?.observation?.limitations || []);

  return <div className={`${styles.page} skin-photo-page`}>
    <Link className={styles.back} href="/skin-report">← 返回膚況日記</Link>
    <header className={styles.heading}><span className={styles.eyebrow}>BLINK PHOTO LAB <span>實驗功能</span></span>
      <h1>用照片，記下今天的膚況。</h1><p>拍一張，或選一張照片，先整理看得到的特徵，再由你確認。</p></header>
    <div className={styles.columns}>
      <section className={styles.card} aria-label="提供膚況照片">
        <div className={styles.cardTitle}><h2>01 <span>提供照片</span></h2><span className={styles.status}>{ready === null ? "確認服務中" : ready ? "可開始測試" : "服務準備中"}</span></div>
        <input ref={fileInput} type="file" accept="image/jpeg,image/png,image/webp" onChange={chooseFile} hidden aria-label="選擇膚況照片檔案" />
        <input ref={nativeCamera} type="file" accept="image/*" capture="user" onChange={chooseFile} hidden aria-label="手機相機照片檔案" />
        {cameraOpen ? <div className={styles.camera}>
          <video ref={video} autoPlay playsInline muted aria-label="相機預覽" />
          <div className={styles.cameraActions}><button type="button" onClick={() => void takePhoto()}>拍下這張</button><button type="button" onClick={stopCamera}>關閉相機</button></div>
        </div> : preview ? <div className={styles.preview}><img src={preview} alt={isSample ? "公開授權的皮膚例圖，不是你的照片" : "已選取、尚待確認的膚況照片"} /><span>{isSample ? "公開例圖・非你的膚況" : "你的照片・只在按下分析後傳送"}</span></div> :
          <div className={styles.empty}><CameraIcon/><strong>從一張清楚的照片開始</strong><p>均勻光線、關閉美肌與濾鏡，<br/>讓想觀察的部位清楚入鏡。</p></div>}
        <div className={styles.actions}><button className={styles.primary} type="button" disabled={busy || preparing || cameraPending} onClick={() => fileInput.current?.click()}>選擇照片</button>
          <button className={styles.secondary} type="button" disabled={busy || preparing || cameraPending} onClick={() => void openCamera()}><CameraIcon/>開啟相機</button></div>
        {cameraPending && <button className={styles.textButton} type="button" onClick={stopCamera}>取消等候相機權限</button>}
        {cameraMessage && <div className={styles.notice} role="status"><p>{cameraMessage}</p><button className={styles.textButton} type="button" disabled={busy} onClick={() => nativeCamera.current?.click()}>使用手機相機或選取照片</button></div>}
        <button className={styles.sample} type="button" disabled={busy || preparing} onClick={() => void analyze(true)}>先用公開例圖，實際測試一次 <span>↗</span></button>
        {isSample && <p className={styles.attribution}>例圖：Dr.Gandikota Raghurama Rao，<a href="https://commons.wikimedia.org/wiki/File:0601_Acne_Vulgaris.jpg" target="_blank" rel="noreferrer">CC BY 4.0</a>。此處只作測試。</p>}
        {preparing && <p role="status">正在處理照片…</p>}
        {image && <div className={styles.analyzeForm}><label htmlFor="photo-note">想補充的感受 <span>選填</span></label>
          <textarea id="photo-note" maxLength={500} disabled={busy} value={note} onChange={event => setNote(event.target.value)} placeholder="例如：這幾天有點刺，洗臉後比較明顯。" />
          <p>按下分析後，照片會傳至 BLINK AI 伺服器處理。原圖不留存，也不會用於訓練。</p>
          <button className={styles.primary} disabled={busy} type="button" onClick={() => void analyze(false)}>{busy ? "正在觀察照片…" : "開始分析這張照片"}</button>
          <button className={styles.textButton} disabled={busy} type="button" onClick={clearPhoto}>移除這張照片</button></div>}
      </section>
      <section className={styles.card} aria-label="照片分析結果" aria-live="polite" aria-busy={busy}>
        <div className={styles.cardTitle}><h2>02 <span>一起看結果</span></h2></div>
        {error ? <div className={styles.error} role="alert"><strong>這次尚未完成</strong><p>{error}</p>{error.includes("登入") && <Link href="/login?next=%2Fskin-photo">前往登入 →</Link>}</div> : busy ?
          <div className={styles.wait}><span className={styles.pulse}/><h3>正在觀察這張照片</h3><p>正在整理可見特徵與照片限制，請稍候。</p></div> : !report ?
          <div className={styles.explain}><span className={styles.smallLabel}>可以先觀察什麼？</span><h3>看得到的，先整理。<br/>不確定的，留待確認。</h3><div className={styles.tags}>{Object.values(labels).map(label => <span key={label}>{label}</span>)}</div><p>結果會包含可能的部位、可見描述與補充問題。照片無法取代你實際的感受，也不是疾病診斷。</p><div className={styles.hint}>還沒準備照片？左側的「公開例圖」會實際呼叫分析，讓你先看流程。</div></div> :
          <div className={styles.result}>
            <span className={styles.smallLabel}>{report.input_source === "public_sample" ? "公開例圖的分析結果" : "這張照片的觀察"}</span>
            <h3>{report.status === "needs_confirmation" ? "這張照片的觀察摘要" : report.status === "invalid_model_output" ? "這次沒有可採用的分析。" : "換一張照片，會更清楚。"}</h3>
            {findingGroups.length > 0 && <p className={styles.summaryLead}>整理出 {findingGroups.length} 類待確認特徵。同類特徵已合併，展開可查看各部位的依據。</p>}
            {findingGroups.map(group => <article className={styles.groupedFinding} key={group.tag}>
              <div className={styles.groupedTitle}><strong>{labels[group.tag] || "待確認特徵"}</strong></div>
              <div className={styles.regionChips} aria-label="涉及部位">{group.regions.map(region => <span key={region}>{regions[region] || "部位待確認"}</span>)}</div>
              {group.findings.length === 1 ? <p className={styles.singleEvidence}>{group.findings[0].evidence}</p> :
                <details className={styles.evidenceDetails}><summary>查看各部位描述 · {group.findings.length}</summary><dl>{group.findings.map((finding,index) => <div key={index}><dt>{regions[finding.region] || "部位待確認"}</dt><dd>{finding.evidence}</dd></div>)}</dl></details>}
            </article>)}
            {report.status === "invalid_model_output" && <p>模型回覆未通過格式檢查，這次結果不會採用。請換一張照片再試。</p>}
            {report.observation?.image_scope === "non_skin" && <p>照片中沒有可供分析的皮膚區域，請改用清楚的膚況照片。</p>}
            {report.status === "needs_confirmation" && report.observation?.observations.length === 0 && <p>這張照片沒有可確認的候選特徵；不代表已確認皮膚正常。</p>}
            {(limitations.length > 0 || report.observation?.follow_up_question) && <div className={styles.nextStep}>
              <strong>接下來可以做什麼</strong>
              {report.observation?.follow_up_question && <p>{report.observation.follow_up_question}</p>}
              {limitations.length > 0 && <details open={report.status !== "needs_confirmation"}><summary>查看判讀限制</summary><ul>{limitations.map((item,index) => <li key={index}>{item}</li>)}</ul></details>}
            </div>}
            <p className={styles.footer}>結果尚未存入日記。{report.inference_seconds ? ` 本次觀察約 ${Math.round(report.inference_seconds)} 秒。` : ""}</p>
            <Link className={styles.diary} href="/skin-record">確認後，前往填寫膚況紀錄 →</Link>
            <a className={styles.researchLink} href="/skin-research/index.html" target="_blank" rel="noreferrer">查看模型訓練與比較紀錄 ↗</a>
          </div>}
      </section>
    </div>
  </div>;
}
