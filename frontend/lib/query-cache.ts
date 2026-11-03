type CacheEntry<T> = {
  expiresAt: number;
  value: T;
};

const values = new Map<string, CacheEntry<unknown>>();
const inFlight = new Map<string, Promise<unknown>>();

export async function queryCached<T>(
  key: string,
  loader: () => Promise<T>,
  ttlMs = 30_000,
): Promise<T> {
  const now = Date.now();
  const cached = values.get(key) as CacheEntry<T> | undefined;
  if (cached && cached.expiresAt > now) return cached.value;
  if (cached) values.delete(key);

  const pending = inFlight.get(key) as Promise<T> | undefined;
  if (pending) return pending;

  const request = loader()
    .then((value) => {
      values.set(key, { value, expiresAt: Date.now() + Math.max(0, ttlMs) });
      return value;
    })
    .finally(() => {
      if (inFlight.get(key) === request) inFlight.delete(key);
    });
  inFlight.set(key, request);
  return request;
}

export function invalidateQuery(keyOrPrefix: string): void {
  for (const key of values.keys()) {
    if (key === keyOrPrefix || key.startsWith(keyOrPrefix)) values.delete(key);
  }
}

export function clearQueryCache(): void {
  values.clear();
  inFlight.clear();
}
