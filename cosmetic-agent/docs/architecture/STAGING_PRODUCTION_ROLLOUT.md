# Staging and production rollout runbook

Status: **Deployment prepared; staging application rollout not started**  
Verified: **2026-10-02**

## Completed before deployment

- The isolated staging Python environment is at `/var/lib/jenkins/tomod/ops/staging/backend-venv`.
- `redis-py 8.1.0`, `redis.asyncio`, and seven required OpenTelemetry modules pass `scripts/verify_backend_runtime.py --packages-only`.
- The isolated Redis configuration is `deploy/staging/redis.conf` on port 6389.
- The persistence exercise wrote job and admission state, forced an AOF rewrite, stopped Redis, restarted it, and verified both values were restored.
- Production launch now fails before starting services if Redis async, OTel packages, Redis connectivity, `APP_RELEASE`, AOF/RDB, maxmemory, noeviction, or the latency monitor is missing.
- `/livez` and `/readyz` are available in the new build. `/metrics` only accepts loopback traffic or a valid bearer token.

## Deployment checkpoint requiring the team

Do not restart the current `next dev` or production backend as part of preparation. A team member must be present before the following state changes:

1. Persist the production Redis settings in the service-managed Redis configuration, not only with `CONFIG SET`:
   - `appendonly yes`, `appendfsync everysec`;
   - a non-empty RDB `save` schedule;
   - a capacity selected from measured headroom, with `maxmemory-policy noeviction`;
   - `latency-monitor-threshold 10` (milliseconds).
2. Take and verify a Redis backup before enabling AOF or changing the capacity limit. Perform a production restore drill in a separate instance; never restore over the live database.
3. Install `requirements-backend.txt` into the release-specific production Python environment and run `scripts/verify_backend_runtime.py` against production Redis.
4. Set a new, unique `APP_RELEASE` in the canonical backend `.env`. Set `EXPECTED_APP_RELEASE` when invoking the launcher so a stale release is rejected.
5. Configure a high-entropy `METRICS_BEARER_TOKEN` for a remote metrics collector. Local scraping may continue through loopback.

## Staging worker matrix

Use the release-specific environment, Redis on port 6389, and a staging-only backend port. Run the same immutable build with 1, 2, and 4 Uvicorn workers. For every row, record p50/p95, error rate, queue wait, first-token time, and event-loop lag.

| Workers | Required checks |
|---:|---|
| 1 | baseline smoke, `/livez`, `/readyz`, release header, metrics access control |
| 2 | concurrent chat, cross-worker job lookup, SSE disconnect/reconnect with `Last-Event-ID` |
| 4 | burst load, one-worker termination, in-flight recovery, queue/admission consistency |

A row passes only when:

- `/readyz` reports every dependency as `ok` and the expected release;
- unauthenticated non-loopback `/metrics` returns 404 and an authenticated scrape returns 200;
- SSE event IDs remain strictly increasing and reconnect neither loses nor duplicates completed events;
- terminating one worker does not lose Redis-backed job or admission state;
- no request silently switches to process memory;
- no 5xx occurs in the smoke suite and p95 does not regress more than 15% from the one-worker baseline without an explained capacity tradeoff.

## Production canary and rolling switch

1. Freeze the release identifier, dependency lock, Redis configuration, and test evidence.
2. Route 5% of eligible traffic to the new release for at least 15 minutes.
3. Continue to 25%, 50%, and 100% only when readiness, 5xx rate, queue wait, SSE reconnect, and Redis memory stay within the staging gates.
4. Drain old workers before termination. Do not kill all workers simultaneously.
5. Roll back traffic immediately on readiness failure, release mismatch, Redis write errors, duplicated SSE events, or sustained p95 regression above 15%.
6. Run `/var/lib/jenkins/tomod/ops/runtime/status-production.sh`; completion requires both `/livez` and `/readyz` success plus version-manifest alignment.

## Current stop point

Preparation is complete through the isolated environment, safety gates, Redis restore exercise, endpoint changes, and unit tests. The next action is the staging application deployment with a new `APP_RELEASE`; stop here until the team confirms the staging port, release value, and traffic path.

## Team handoff and local safety tooling

- The exact five-owner checklist is in [STAGING_TEAM_HANDOFF.md](STAGING_TEAM_HANDOFF.md).
- scripts/verify_staging_isolation.py refuses production ports, public binds, memory fallback, shared production state identifiers, unsafe OAuth callbacks, production cookie names, and wildcard or production frontend origins.
- deploy/staging/run-backend.sh only accepts 1, 2, or 4 workers and runs package plus isolation checks before Uvicorn can bind.
- scripts/staging_endpoint_smoke.py checks release identity, readiness dependencies, and the three metrics authorization cases.
- tests/load/sse_reconnect.py interrupts a real stream and reconnects with the same Idempotency-Key plus Last-Event-ID, then verifies unique event IDs and terminal job state.
- Do not run the launcher until the team has injected isolated PostgreSQL, Zep, Neo4j, Redis, signing, and metrics credentials. The expected guard failure against the current production-valued environment is evidence that no staging process was accidentally started.

