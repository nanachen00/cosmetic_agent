# BLINK dependency map

Verified: **2026-09-25**

## Layer map

```text
Nginx
  -> api/api_server.py (current ASGI compatibility entry)
       -> app/api/routes/*
       -> app/security/*
       -> agents/root/orchestrator.py
       -> agents/{knowledge,product,skin_tracking}/*
       -> tools/{auth,getzep,search,user,agent/tracking}/*
       -> database/postgres/repositories/*
       -> core/{observability,tracing,usage_tracking}.py
```

## Current route ownership

Extracted route modules include health, admin, auth, OAuth, daily content, password recovery, email registration, quiz, chat, conversations, skin records and daily check-in. Skin tracking is installed by `agents/skin_tracking/fastapi_integration.py`. `api/api_server.py` has no direct route decorators; it remains the composition root that injects shared auth, guard, admission and agent dependencies.

## Infrastructure dependencies

| Dependency | Consumer | Failure effect |
|---|---|---|
| PostgreSQL | Auth, identities, conversations, skin/check-in records, usage | 8010 not ready; writes and identity fail |
| Redis | Rate limiting and shared runtime controls | Readiness fails when required; overload protection may fail closed |
| Neo4j | Graph RAG / knowledge retrieval | Knowledge answers degrade or fail; currently not a top-level readiness item |
| BGE-M3 :8002 | Knowledge/product vectorization | Semantic retrieval unavailable |
| TAIDE :8000 | Current model generation and health | Chat generation unavailable |
| TAIDE Router :8001 | Routed model path | Router-dependent requests unavailable |
| LiteLLM :8003 | OpenAI-compatible `taide-12b` proxy | Proxy clients unavailable |
| Google/LINE OAuth | Login | Provider-specific login unavailable |
| Better Stack/OpenTelemetry | Monitoring | Reduced observability; app should remain functional |

## Configuration dependencies

- `APP_ENV=production`
- `TAIDE_BASE_URL` currently resolves to local port 8000.
- `GRAPHRAG_API_BASE` currently resolves to local port 8000 and the configured
  Graph RAG model is `taide-12b`.
- `CHAT_LIMIT_REQUIRE_REDIS=1` means Redis is required for chat-limit enforcement.
- Database and Redis URLs are configured in `.env` and must never be documented
  with credential values.

## Confirmed cleanup facts

- Active Python source contains no MongoDB driver/URI references.
- Backup-like files, Notebook checkpoints, Codex patch files and GPU core dumps
  are not valid production import targets.
- Duplicate repository basenames currently include identity, email registration,
  OAuth identity, password auth and password reset. These are migration debt, not
  dead code; keep them until all importers use the PostgreSQL canonical path.
