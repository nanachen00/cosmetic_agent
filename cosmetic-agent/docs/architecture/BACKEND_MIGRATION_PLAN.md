# Backend migration plan

Decision: **Approve the proposed Router → Service → Repository/Client layering,
but adapt it to the existing repository instead of creating a second `backend/`
tree.**

## Target layout

```text
cosmetic-agent/
├── api/
│   └── api_server.py              # temporary compatibility ASGI entry
├── app/
│   ├── main.py                    # future composition root
│   ├── api/
│   │   ├── routes/                # thin FastAPI handlers
│   │   └── schemas/               # transport request/response models
│   ├── services/                  # business workflows
│   ├── clients/                   # Redis/model/embedding/external HTTP clients
│   ├── middleware/                # request ID, timing, errors, rate limit
│   └── security/                  # auth guards and dependencies
├── agents/                        # AI/domain agents, not HTTP plumbing
├── core/                          # config, tracing, logging, shared settings
├── database/
│   └── postgres/
│       ├── repositories/          # canonical persistence boundary
│       └── migrations/
├── schemas/                       # legacy compatibility, gradually re-export
└── tests/
```

## Why not create `backend/` now?

The repository already contains most of the proposed layers. Adding `backend/`
would create a third location for routes, schemas, security and repositories,
making the exact stale-information problem worse. The correct change is to finish
the existing structure and reduce `api/api_server.py` gradually.

## Migration sequence

### Phase 0 — guardrails and inventory (completed 2026-09-25)

- Establish canonical architecture documents and repository instructions.
- Archive only non-importable snapshots/generated artifacts with hashes and a
  restore script.
- Keep the current ASGI target and runtime topology unchanged.

### Phase 1 — centralize clients

Create `app/clients/` adapters for TAIDE, router, embedding, Redis and external
HTTP services. Move URL, timeout, retry, header and overload behavior into these
adapters without changing route behavior. Add contract tests before switching a
caller.

### Phase 2 — settle repository ownership

Choose `database/postgres/repositories/` as canonical. Convert
`database/repositories/` modules into temporary compatibility re-exports, migrate
imports one domain at a time, then remove a shim only after repository-wide import
search and tests show zero callers.

### Phase 3 — extract services and routes

Recommended order:

1. Conversations/history
2. Skin records/reports
3. Chat orchestration and streaming
4. Remaining compatibility endpoints

For each group: characterize existing behavior, add tests, extract service,
extract route, keep endpoint contract, verify readiness and rollback.

### Phase 4 — composition root

Create `app/main.py` to build the FastAPI app and register middleware/routers.
Keep `api/api_server.py` as a compatibility re-export:

```python
from app.main import app
```

Change the runtime ASGI target only after both imports produce the same route set
and a production-like smoke test passes.

### Phase 5 — remove compatibility code

After at least one observed production cycle, remove shims backed by import/search
evidence. Never mix compatibility removal with a model-server migration.

## Required acceptance checks per phase

- Python syntax and focused tests pass.
- Route paths and response schemas are unchanged unless versioned.
- `/readyz` reports all required dependencies as OK.
- CORS, Google OAuth and LINE OAuth startup validations pass.
- Streaming and non-streaming chat both work.
- Runtime PID/command/cwd/version checks pass.
- A rollback is documented before deployment.

### Phase 1 progress — 2026-09-25

- Added `app/clients/embedding_client.py` as the owner of port 8002 calls.
- Added persistent HTTP connection pooling with hidden POST retries disabled.
- Migrated `agents/product/product_agent.py` to the shared client while keeping
  its existing public function and error contract.
- Removed the dead in-process embedding fallback hook; production will not load
  a second embedding model when port 8002 is unavailable.
- Added isolated client contract tests and a product-agent delegation test.

### Phase 2 progress — 2026-09-25

- Added `app/clients/chat_completion_client.py` for OpenAI-compatible model calls.
- Migrated the active product ingredient batch call without changing its model,
  prompt, endpoint configuration or fallback result.
- Removed the unused per-product TAIDE extraction function, leaving one batch
  model call for at most five products.
- Added connection pooling, response validation and an explicit no-hidden-retry
  contract. Router cutover remains a separate measured change.

### Phase 3 progress — 2026-09-25

- Migrated Root Orchestrator rank-feedback TAIDE generation to the shared
  `ChatCompletionClient` without changing its model, prompt, token limit,
  temperature, timeout or deterministic fallback.
- Removed the Orchestrator's ad-hoc `requests.post` model call. The main Terra
  pain/gain extraction remains unchanged and is intentionally outside this step.
- Added focused tests proving one model call on success and one failed call
  followed by the existing local fallback, with no hidden retry.

