export type PhotoAvailabilityStatus = "unknown" | "ready" | "unavailable" | "login_required" | "auth_unavailable";
export type PhotoAvailability = { status: PhotoAvailabilityStatus; checking: boolean };
type HealthResponse = { status: number; ok: boolean; json(): Promise<unknown> };
type Timer = ReturnType<typeof setTimeout> | number;
type Options = {
  fetcher?: (signal: AbortSignal) => Promise<HealthResponse>;
  isVisible?: () => boolean;
  isOnline?: () => boolean;
  setTimer?: (callback: () => void, delay: number) => Timer;
  clearTimer?: (timer: Timer) => void;
  timeoutMs?: number;
  retryMs?: number;
};

export function classifyPhotoAvailability(status: number, payload: unknown): PhotoAvailabilityStatus {
  const body = payload && typeof payload === "object" ? payload as { error?: unknown; ready?: unknown } : {};
  if (status === 401 || status === 403 || body.error === "login_required") return "login_required";
  if (body.error === "auth_unavailable") return "auth_unavailable";
  return status >= 200 && status < 300 && body.ready === true ? "ready" : "unavailable";
}

/** One bounded GET, with retries only while visible, online, and unavailable. */
export function createPhotoAvailabilityMonitor(onChange: (state: PhotoAvailability) => void, options: Options = {}) {
  const fetcher = options.fetcher ?? (signal => fetch("/api/skin-photo", { cache: "no-store", signal }));
  const visible = options.isVisible ?? (() => !document.hidden);
  const online = options.isOnline ?? (() => navigator.onLine !== false);
  const setTimer = options.setTimer ?? setTimeout;
  const clearTimer = options.clearTimer ?? clearTimeout;
  const timeoutMs = options.timeoutMs ?? 4000;
  const retryMs = options.retryMs ?? 30000;
  let stopped = false;
  let last: PhotoAvailability = { status: "unknown", checking: false };
  let retry: Timer | null = null;
  let current: Promise<PhotoAvailability | undefined> | null = null;
  let cancelCurrent: (() => void) | null = null;

  function clearRetry() { if (retry !== null) clearTimer(retry); retry = null; }
  function publish(state: PhotoAvailability) { if (!stopped) { last = state; onChange(state); } }
  function schedule() {
    clearRetry();
    if (!stopped && visible() && online() && ["unknown", "unavailable"].includes(last.status)) {
      retry = setTimer(() => { retry = null; void check(); }, retryMs);
    }
  }
  function setStatus(status: PhotoAvailabilityStatus) {
    if (stopped) return;
    cancelCurrent?.(); clearRetry();
    publish({ status, checking: false }); schedule();
  }
  function check(): Promise<PhotoAvailability | undefined> {
    if (stopped || !visible()) return Promise.resolve(undefined);
    if (!online()) { setStatus("unavailable"); return Promise.resolve(last); }
    if (current) return current;
    clearRetry(); publish({ status: "unknown", checking: true });
    const controller = new AbortController();
    let resolve: (state: PhotoAvailability | undefined) => void;
    const pending = new Promise<PhotoAvailability | undefined>(done => { resolve = done; });
    current = pending;
    let finished = false;
    const finish = (state?: PhotoAvailability) => {
      if (finished) return;
      finished = true; clearTimer(deadline); current = null; cancelCurrent = null;
      if (!stopped && state) { publish(state); schedule(); }
      resolve(stopped ? undefined : state);
    };
    const deadline = setTimer(() => { controller.abort(); finish({ status: "unavailable", checking: false }); }, timeoutMs);
    cancelCurrent = () => { controller.abort(); finish(); };
    void (async () => {
      try {
        const response = await fetcher(controller.signal);
        // An unauthenticated HTML response must not become a model outage.
        const payload = response.status === 401 || response.status === 403 ? null : await response.json();
        finish({ status: classifyPhotoAvailability(response.status, payload), checking: false });
      } catch { finish({ status: "unavailable", checking: false }); }
    })();
    return pending;
  }
  function pause() { clearRetry(); cancelCurrent?.(); publish({ status: "unknown", checking: false }); }
  function dispose() { stopped = true; clearRetry(); cancelCurrent?.(); }
  return { check, pause, setStatus, dispose };
}
