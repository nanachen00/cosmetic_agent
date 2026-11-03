# BLINK AI source backup manifest

Backup date: 2026-11-03 (Asia/Taipei)

This repository contains the sanitized, restorable BLINK AI platform source from the production workspace.

## Included

- `frontend/`: Next.js mobile/web frontend
- `cosmetic-agent/`: FastAPI backend, agents, auth, knowledge map, persistence and tests
- `core/`, `src/`, `supervisor/`: shared runtime and service configuration
- `ops/`: deployment, validation, migration and benchmark source
- `regression_tests/`: regression checks
- `experiments/`: research code and reproducible configuration (without datasets or model output)
- root service scripts and recovery documentation
- `recovery/`: previously committed idempotent knowledge-map recovery patches

## Intentionally excluded

- `.env*`, OAuth/API/SMTP credentials, tokens and private keys
- PostgreSQL/Redis/SQLite data, user profiles, conversations and uploads
- model weights, checkpoints and generated adapters
- research datasets, training output and duplicated release/archive snapshots
- dependency directories, build output, caches and logs

These exclusions protect users and keep the repository suitable for source control. Recreate secrets from `.env.example` and restore databases from an approved private backup, never from this public repository.

## Verification snapshot

- Semantic regression: 44/44 passed
- Human-language scenarios: 100/100 passed
- Backend knowledge tests: 49 passed
- Frontend personalization contract: passed
- TypeScript: passed
- Next.js production build: passed
- Mobile widths 320 / 390 / 430 px: no horizontal overflow
