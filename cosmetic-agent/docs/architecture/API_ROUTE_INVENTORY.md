# API Route Inventory

This is the canonical evidence ledger for API retention. It is generated from the live :8010 OpenAPI document and a static scan of the current frontend source.

## Safety boundary

- No endpoint is authorized for deletion by this document.
- KEEP_PROTECTED covers OAuth, authentication, Admin, health, readiness and metrics routes even when the browser frontend has no direct reference.
- OBSERVE means traffic evidence is still required; it does not mean unused.
- AI-Stack is administered externally. Port 8200 is experimental/unmanaged and was not inspected, restarted or changed.

## Baseline

- Live OpenAPI method/path combinations: 70
- Frontend source files scanned: 44
- KEEP_PROTECTED: 37
- KEEP_FRONTEND: 25
- OBSERVE_DOMAIN: 5
- OBSERVE_UNKNOWN: 3

## Decision meanings

| Decision | Meaning | Current action |
|---|---|---|
| KEEP_PROTECTED | Infrastructure, OAuth, authentication or Admin route | Keep; verify through its owning workflow |
| KEEP_FRONTEND | Static evidence exists in current frontend source | Keep |
| OBSERVE_DOMAIN | Known domain route without a static frontend hit | Keep while collecting runtime evidence |
| OBSERVE_UNKNOWN | No owner and no current frontend hit | Investigate before any deprecation |

## Route ledger

| Method | Path | Owner | Frontend evidence | Decision |
|---|---|---|---|---|
| POST | /api/admin/costs/pricing | ADMIN | app/admin/costs/page.tsx | KEEP_PROTECTED |
| GET | /api/admin/costs/summary | ADMIN | app/admin/costs/page.tsx | KEEP_PROTECTED |
| GET | /api/admin/daily-contents | ADMIN | app/admin/content/page.tsx | KEEP_PROTECTED |
| GET | /api/admin/daily-contents/stats | ADMIN | app/admin/content/page.tsx | KEEP_PROTECTED |
| PATCH | /api/admin/daily-contents/{content_id} | ADMIN | app/admin/content/page.tsx | KEEP_PROTECTED |
| GET | /api/admin/data-health | ADMIN | app/admin/page.tsx | KEEP_PROTECTED |
| GET | /api/admin/gpu | ADMIN | app/admin/page.tsx | KEEP_PROTECTED |
| GET | /api/admin/knowledge | ADMIN | app/admin/page.tsx | KEEP_PROTECTED |
| GET | /api/admin/overview | ADMIN | app/admin/page.tsx | KEEP_PROTECTED |
| GET | /api/admin/session | ADMIN | none found by static scan | KEEP_PROTECTED |
| GET | /api/admin/users | ADMIN | app/admin/page.tsx | KEEP_PROTECTED |
| GET | /api/admin/users/{user_id} | ADMIN | app/admin/page.tsx | KEEP_PROTECTED |
| PATCH | /api/admin/users/{user_id}/status | ADMIN | app/admin/page.tsx | KEEP_PROTECTED |
| GET | /api/auth/google/callback | OAUTH | none found by static scan | KEEP_PROTECTED |
| GET | /api/auth/google/login | OAUTH | none found by static scan | KEEP_PROTECTED |
| POST | /api/auth/guest-session | AUTH | none found by static scan | KEEP_PROTECTED |
| POST | /api/auth/identity | AUTH | none found by static scan | KEEP_PROTECTED |
| GET | /api/auth/line/callback | OAUTH | none found by static scan | KEEP_PROTECTED |
| GET | /api/auth/line/login | OAUTH | none found by static scan | KEEP_PROTECTED |
| POST | /api/auth/logout | AUTH | none found by static scan | KEEP_PROTECTED |
| GET | /api/auth/onboarding/status | AUTH | none found by static scan | KEEP_PROTECTED |
| POST | /api/auth/onboarding/username | AUTH | none found by static scan | KEEP_PROTECTED |
| POST | /api/auth/password/change | AUTH | none found by static scan | KEEP_PROTECTED |
| POST | /api/auth/password/login | AUTH | none found by static scan | KEEP_PROTECTED |
| POST | /api/auth/password/recovery/confirm | AUTH | lib/account-api.ts | KEEP_PROTECTED |
| POST | /api/auth/password/recovery/request | AUTH | lib/account-api.ts | KEEP_PROTECTED |
| POST | /api/auth/password/register | AUTH | none found by static scan | KEEP_PROTECTED |
| POST | /api/auth/password/register/confirm | AUTH | none found by static scan | KEEP_PROTECTED |
| POST | /api/auth/password/register/request | AUTH | none found by static scan | KEEP_PROTECTED |
| POST | /api/auth/password/reset/confirm | AUTH | none found by static scan | KEEP_PROTECTED |
| POST | /api/auth/password/reset/request | AUTH | none found by static scan | KEEP_PROTECTED |
| POST | /api/auth/password/set | AUTH | none found by static scan | KEEP_PROTECTED |
| POST | /api/auth/password/verify | AUTH | none found by static scan | KEEP_PROTECTED |
| DELETE | /api/auth/providers/{provider} | AUTH | none found by static scan | KEEP_PROTECTED |
| GET | /api/auth/session | AUTH | none found by static scan | KEEP_PROTECTED |
| PATCH | /api/auth/username | AUTH | none found by static scan | KEEP_PROTECTED |
| GET | /api/auth/username/availability | AUTH | none found by static scan | KEEP_PROTECTED |
| GET | /api/chat/history | CHAT | lib/api.ts | KEEP_FRONTEND |
| POST | /api/chat/message | CHAT | lib/api.ts | KEEP_FRONTEND |
| POST | /api/chat/message/stream | CHAT | lib/api.ts | KEEP_FRONTEND |
| POST | /api/checkin | UNKNOWN | app/daily-checkin/page.tsx<br>components/CustomerQuiz.tsx<br>lib/api.ts<br>lib/checkin-api.ts | KEEP_FRONTEND |
| GET | /api/checkin/history | CHECKIN | lib/checkin-api.ts | KEEP_FRONTEND |
| GET | /api/checkin/status | CHECKIN | lib/checkin-api.ts | KEEP_FRONTEND |
| GET | /api/conversations | CONVERSATION | lib/api.ts | KEEP_FRONTEND |
| POST | /api/conversations | CONVERSATION | lib/api.ts | KEEP_FRONTEND |
| DELETE | /api/conversations/{conversation_id} | CONVERSATION | lib/api.ts | KEEP_FRONTEND |
| PATCH | /api/conversations/{conversation_id} | CONVERSATION | lib/api.ts | KEEP_FRONTEND |
| GET | /api/daily-contents | DAILY_CONTENT | app/admin/content/page.tsx<br>lib/checkin-api.ts | KEEP_FRONTEND |
| GET | /api/quiz/latest | QUIZ | lib/api.ts<br>lib/quiz-api.ts | KEEP_FRONTEND |
| GET | /api/quiz/questions | QUIZ | lib/api.ts<br>lib/quiz-api.ts | KEEP_FRONTEND |
| POST | /api/quiz/submit | QUIZ | components/CustomerQuiz.tsx<br>lib/api.ts<br>lib/quiz-api.ts | KEEP_FRONTEND |
| GET | /api/skin-records | UNKNOWN | lib/api.ts | KEEP_FRONTEND |
| POST | /api/skin-records | UNKNOWN | lib/api.ts | KEEP_FRONTEND |
| POST | /api/skin-records/frontend | UNKNOWN | lib/api.ts | KEEP_FRONTEND |
| DELETE | /api/skin-records/{record_id} | UNKNOWN | lib/api.ts | KEEP_FRONTEND |
| PATCH | /api/skin-records/{record_id} | UNKNOWN | lib/api.ts | KEEP_FRONTEND |
| GET | /api/skin-reports/current | UNKNOWN | none found by static scan | OBSERVE_UNKNOWN |
| POST | /api/skin-tracking/message | SKIN_TRACKING | lib/skin-tracking-api.ts | KEEP_FRONTEND |
| GET | /api/skin-tracking/records | SKIN_TRACKING | none found by static scan | OBSERVE_DOMAIN |
| DELETE | /api/skin-tracking/records/{record_id} | SKIN_TRACKING | none found by static scan | OBSERVE_DOMAIN |
| PATCH | /api/skin-tracking/records/{record_id} | SKIN_TRACKING | none found by static scan | OBSERVE_DOMAIN |
| GET | /api/skin-tracking/reports/current | SKIN_TRACKING | lib/skin-tracking-api.ts | KEEP_FRONTEND |
| GET | /api/skin-tracking/reports/recommendations | SKIN_TRACKING | lib/skin-tracking-api.ts | KEEP_FRONTEND |
| GET | /api/skin-tracking/sessions/{session_id} | SKIN_TRACKING | none found by static scan | OBSERVE_DOMAIN |
| PUT | /api/user/profile | UNKNOWN | lib/api.ts | KEEP_FRONTEND |
| POST | /api/v1/chat | CHAT | none found by static scan | OBSERVE_DOMAIN |
| GET | /api/v1/health | UNKNOWN | none found by static scan | OBSERVE_UNKNOWN |
| GET | /api/v1/profile | UNKNOWN | lib/api.ts | KEEP_FRONTEND |
| POST | /api/v1/session/clear | UNKNOWN | lib/api.ts | KEEP_FRONTEND |
| GET | /api/v1/welcome | UNKNOWN | none found by static scan | OBSERVE_UNKNOWN |

## Safe retirement gate

A route may be proposed for retirement only after all gates pass:

1. Normalized route metrics show zero traffic for an agreed observation window (recommended 14 to 30 days).
2. It is not required by OAuth callbacks, Admin, schedulers, health checks, monitoring or service-to-service calls.
3. Repository-wide source and startup-script searches find no callers.
4. Deprecation is announced first; route registration is removed before source is archived or deleted.
5. Route uniqueness checks, full backend tests, frontend build, /readyz and production version alignment all pass.

## Runtime evidence note

The application exports normalized per-route Prometheus counters and one structured http_request log event per request. In-process counters still reset when :8010 restarts, but start-production.sh now appends to backend-8010.log instead of truncating it, so normalized route evidence survives managed restarts.

Generate a 30-day report without restarting services:

    cd /var/lib/jenkins/tomod/cosmetic-agent
    python scripts/report_api_route_usage.py --days 30

The report contains request count, 5xx count, p95 latency and last-seen time for each normalized method/route. It intentionally does not read request bodies, user messages, tokens or raw dynamic URL identifiers. Monitor the log directory size and archive logs during an approved maintenance window; a single short observation window is not sufficient deletion evidence.

## Refresh procedure

Regenerate this ledger after routes or frontend call sites change. Compare the generated OpenAPI list with the previous committed version and preserve protected categories.
