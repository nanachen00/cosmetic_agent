# Temporal freshness routing

Status: implemented and tested in source; not deployed  
Date: 2026-10-02

## Purpose

The chat pipeline must distinguish stable knowledge from facts that change with time, place, version, inventory, or an external event. It must not answer a live question from model memory or silently infer location from IP.

## Request path

1. A deterministic classifier runs before Input Guard and before Terra/LLM inference.
2. It separates domain from temporal scope: live, historical, forecast, versioned, or stable.
3. It extracts explicit slots. Weather and air quality require a location.
4. Stable and unrelated messages continue through the existing path with no network request.
5. Live weather uses the async Open-Meteo client before the synchronous Root Agent threadpool call.
6. A bounded skincare policy converts temperature, humidity, UV, and rain into advice; the response includes the observation time and source.
7. Missing, historical, forecast, stale, timed-out, or failed provider data is not guessed.

All three chat entrypoints use the same preparation path. TEMPORAL_FRESHNESS_GUARD_ENABLED=0 disables both routing and provider calls for immediate rollback. No process has been restarted and no production environment was changed.

## Coverage

The first-stage classifier is beauty-scoped: skin environment (weather and air quality), cosmetic product availability and price, product safety/recall notices, cosmetic regulations, and beauty-specific official news. Traffic, transit, finance, sports, public-office status, software versions, and general news do not enter this path. Each decision also carries answer_intent and requested_fields so the provider and response layer know whether the user asked for a single metric, a skincare adjustment, stock, price, safety status, or regulatory status.

## Async provider guarantees

- Four-second timeout and a circuit breaker after repeated failure.
- Per-location TTL cache; expired values are revalidated and are never served as current.
- Single-flight coalescing so concurrent identical requests share one provider call.
- Cancellation shielding for shared waiters; the provider task is cancelled when the last waiter leaves.
- Every result includes observed_at, retrieved_at, valid_until, dataset ID, source URL, and cache status.

The current implementation reuses the provider already used by the homepage. Open-Meteo requires attribution and its free/open-access terms must be checked against the deployment's usage. A CWA adapter can later implement the same interface if the team supplies an authorization key.

## Latency and validation

On the staging notebook environment, 10,000 iterations over six representative Chinese queries measured classifier median 6.19 microseconds and p95 12.41 microseconds per query. This is a microbenchmark, not an end-to-end latency claim.

Targeted validation: 62 tests plus 10 subtests passed, including classification scope, false positives, location normalization, metadata, TTL revalidation, cache hits, eight-way request coalescing, circuit breaking, three chat entrypoints, streaming cleanup, and rollback. The five modified runtime modules also pass py_compile.

The repository venv currently lacks pytest; tests were run with the Jupyter Python environment. Rebuilding the staging environment remains required before deployment.

## Deployment acceptance

- 現在天氣幾度 asks for a city/county without calling a provider.
- 台北現在幾度 returns only the requested temperature with timestamp and source.
- 依今天台北天氣調整我的保養 returns the relevant environment evidence and bounded skincare advice.
- 昨天臺北的天氣 is identified as historical and is not answered from the live endpoint.
- 天氣如何影響皮膚 follows the stable knowledge path.
- 我目前臉很乾 remains user-state context, not live external data.
- Staging records provider p50/p95, cache-hit rate, first-token latency, timeout/error rate, and cancellation release time before production rollout.

## Research basis

- FreshQA/FreshPrompt: https://arxiv.org/abs/2310.03214
- Self-RAG: https://arxiv.org/abs/2310.11511
- Toolformer: https://arxiv.org/abs/2302.04761
- Temporal QA survey: https://aclanthology.org/2026.acl-long.1332/
- RFC 9111 freshness semantics: https://datatracker.ietf.org/doc/html/rfc9111
