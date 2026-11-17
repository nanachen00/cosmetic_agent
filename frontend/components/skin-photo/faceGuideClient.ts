export type FaceGuidePoint = { x: number; y: number };
export type FaceGuideFrame = { faces: FaceGuidePoint[][] };
type Bitmap = { close(): void };
type WorkerPort = Pick<Worker, "postMessage" | "terminate" | "onmessage" | "onerror">;

/** One transferred frame at a time; disposal cancels pending work and bounds shutdown. */
export class FaceGuideClient {
  private sequence = 0;
  private closed = false;
  private terminated = false;
  private shutdownTimer: ReturnType<typeof setTimeout> | null = null;
  private pending: { id: number; resolve(value: FaceGuideFrame): void; reject(reason: Error): void; timer: ReturnType<typeof setTimeout> } | null = null;

  constructor(private worker: WorkerPort, private timeoutMs = 20000, private closeGraceMs = 100) {
    worker.onmessage = event => {
      const data = event.data;
      if (data?.type === "closed") { this.dispose(); this.terminate(); return; }
      if (this.closed || !this.pending || data?.id !== this.pending.id) return;
      const pending = this.pending;
      this.pending = null; clearTimeout(pending.timer);
      if (data.type === "result" && Array.isArray(data.faces) && data.faces.length <= 2 && data.faces.every((face: unknown) => Array.isArray(face) && face.length <= 512 && face.every(point => point && Number.isFinite(point.x) && Number.isFinite(point.y)))) {
        pending.resolve({ faces: data.faces });
      } else pending.reject(new Error("landmarks_unavailable"));
    };
    worker.onerror = () => this.dispose();
  }

  analyze(bitmap: Bitmap): Promise<FaceGuideFrame> {
    if (this.closed || this.pending) { bitmap.close(); return Promise.reject(new Error("frame_not_accepted")); }
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => this.dispose(), this.timeoutMs);
      this.pending = { id, resolve, reject, timer };
      try { this.worker.postMessage({ type: "frame", id, bitmap }, [bitmap as Transferable]); }
      catch { bitmap.close(); this.dispose(); }
    });
  }

  dispose(): void {
    if (this.closed) return;
    this.closed = true;
    if (this.pending) { clearTimeout(this.pending.timer); this.pending.reject(new Error("guide_closed")); this.pending = null; }
    // Let the task close its WASM heap, with termination as a bounded fallback.
    this.shutdownTimer = setTimeout(() => this.terminate(), this.closeGraceMs);
    try { this.worker.postMessage({ type: "close" }); } catch { this.terminate(); }
  }

  private terminate(): void {
    if (this.terminated) return;
    this.terminated = true;
    if (this.shutdownTimer !== null) clearTimeout(this.shutdownTimer);
    this.shutdownTimer = null;
    this.worker.onmessage = null; this.worker.onerror = null; this.worker.terminate();
  }
}
