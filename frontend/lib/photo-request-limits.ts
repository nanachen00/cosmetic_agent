/** Resource limits for the single Node photo proxy process. No image logging. */
export class PhotoRequestError extends Error {
  constructor(public readonly status: number, public readonly code: string) {
    super(code);
  }
}

export function photoModelOrigin(configured: string): string | null {
  try {
    const url = new URL(configured);
    if (!["http:", "https:"].includes(url.protocol) || !["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)) return null;
    if (url.username || url.password || url.search || url.hash || url.pathname !== "/") return null;
    return url.origin;
  } catch { return null; }
}

export class PhotoRequestGate {
  private active = 0;
  private readonly visits = new Map<string, number[]>();

  constructor(
    private readonly maxActive = 4,
    private readonly maxUsers = 4096,
    private readonly maxVisits = 4,
    private readonly windowMs = 60000,
  ) {
    if (![maxActive, maxUsers, maxVisits, windowMs].every(value => Number.isInteger(value) && value > 0)) {
      throw new RangeError("Invalid photo proxy limits");
    }
  }

  enter(): (() => void) | null {
    if (this.active >= this.maxActive) return null;
    this.active += 1;
    let released = false;
    return () => {
      if (!released) { released = true; this.active -= 1; }
    };
  }

  takeUserQuota(userId: string, now = Date.now()): void {
    const recent = (this.visits.get(userId) || []).filter(value => now - value < this.windowMs);
    if (recent.length >= this.maxVisits) throw new PhotoRequestError(429, "rate_limited");
    if (!this.visits.has(userId) && this.visits.size >= this.maxUsers) {
      for (const [key, values] of this.visits) {
        if (!values.some(value => now - value < this.windowMs)) this.visits.delete(key);
      }
      if (this.visits.size >= this.maxUsers) throw new PhotoRequestError(503, "rate_limit_capacity");
    }
    this.visits.set(userId, [...recent, now]);
  }
}

export async function readPhotoJson(
  stream: ReadableStream<Uint8Array>,
  options: { maxBytes?: number; timeoutMs?: number; signal?: AbortSignal } = {},
): Promise<unknown> {
  const { maxBytes = 5 * 1024 * 1024, timeoutMs = 20000, signal } = options;
  if (signal?.aborted) throw new PhotoRequestError(400, "request_aborted");
  const reader = stream.getReader();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let stop: (error: PhotoRequestError) => void = () => {};
  const stopped = new Promise<never>((_resolve, reject) => { stop = reject; });
  const cancel = () => { void reader.cancel().catch(() => {}); };
  const abort = () => { stop(new PhotoRequestError(400, "request_aborted")); cancel(); };
  timer = setTimeout(() => { stop(new PhotoRequestError(408, "request_timeout")); cancel(); }, timeoutMs);
  signal?.addEventListener("abort", abort, { once: true });
  const deadline = performance.now() + timeoutMs;
  try {
    // One bounded allocation prevents millions of tiny chunks from growing an object array.
    const bytes = new Uint8Array(maxBytes);
    let length = 0;
    while (true) {
      if (performance.now() >= deadline) throw new PhotoRequestError(408, "request_timeout");
      const part = await Promise.race([reader.read(), stopped]);
      if (part.done) break;
      if (part.value.byteLength > maxBytes - length) throw new PhotoRequestError(413, "image_too_large");
      bytes.set(part.value, length);
      length += part.value.byteLength;
    }
    return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes.subarray(0, length)));
  } catch (error) {
    if (error instanceof PhotoRequestError) throw error;
    throw new PhotoRequestError(400, "invalid_request");
  } finally {
    if (timer !== undefined) clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
    cancel(); // Never await a client-controlled cancellation callback.
    try { reader.releaseLock(); } catch { /* Cancellation may still settle a read. */ }
  }
}
