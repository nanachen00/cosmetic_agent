<!-- BLINK_CANONICAL_ARCHITECTURE -->
# BLINK canonical architecture

Status: **Current source of truth**  
Verified: **2026-11-15 (Asia/Taipei)**

Deployment now splits Mac frontend/backend from school MI300X model services.
See `docs/architecture/PLATFORM_DELIVERY_PLAN.md` for current verification and
`../ops/runtime/SPLIT_DEPLOYMENT.md` for launch instructions. Model APIs reach
the Mac through SSH loopback forwards; the earlier all-in-one host layout below
is retained only as a path/history reference.

This file is the entry point for architecture decisions. Before changing the
backend, read the documents in [`docs/architecture/`](docs/architecture/README.md).
Dated documents elsewhere in `docs/` are historical or feature-specific unless
the architecture index explicitly promotes them.

## Production control plane

- Start: `/var/lib/jenkins/tomod/ops/runtime/start-production.sh`
- Stop: `/var/lib/jenkins/tomod/ops/runtime/stop-production.sh`
- Status: `/var/lib/jenkins/tomod/ops/runtime/status-production.sh`
- Restart: `/var/lib/jenkins/tomod/ops/runtime/restart-production.sh`
- Backend environment: `/var/lib/jenkins/tomod/cosmetic-agent/.env` only
- Backend and Auth: port `8010`; port `8011` is not part of the production topology
- vLLM port `8200`: internal MI300X inference engine, managed by
  `ops/runtime/start-mi300x-models.sh`; only gateway `8000` is forwarded to Mac.

Do not launch production services with ad-hoc `nohup`, direct `uvicorn`, or an
old root-level restart script. Root-level legacy scripts are compatibility
wrappers that delegate to the runtime control plane.

## Code ownership

| Concern | Canonical location today | Direction |
|---|---|---|
| ASGI compatibility entry point | `api/api_server.py` | Keep thin; migrate registration to `app/main.py` later |
| HTTP routes | `app/api/routes/` plus remaining routes in `api/api_server.py` | Move one route group at a time |
| Cross-request security | `app/security/` | Evolve into middleware/dependencies without duplicating auth |
| Domain orchestration | `agents/` and selected `tools/` modules | Extract reusable business flows to `app/services/` |
| PostgreSQL access | `database/postgres/repositories/` | Canonical repository implementation |
| Database migrations | `database/postgres/migrations/` | Canonical schema history |
| API schemas | `app/api/schemas/` and `schemas/` | Consolidate by compatibility migration |
| Shared runtime concerns | top-level `core/` | Do not create a second `app/core/` yet |
| External/model calls | currently scattered | Centralize in `app/clients/` |

Never use `.bak`, `.old`, `.orig`, Notebook checkpoint, patch, crash dump, or
files under `/var/lib/jenkins/tomod/ops/archive/` as runtime source.
