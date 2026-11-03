# BLINK AI — platform source backup

This repository stores the sanitized BLINK AI platform source used by the mobile/web frontend, backend agents, authentication, persistence, knowledge map, tests and deployment tooling.

- Platform operations: [`PLATFORM_README.md`](PLATFORM_README.md)
- Recovery architecture: [`RESTORE_README.md`](RESTORE_README.md)
- Backup scope and exclusions: [`BACKUP_MANIFEST.md`](BACKUP_MANIFEST.md)

## Verified on 2026-11-03

- Semantic regression: **44/44 passed**
- Human-language scenarios: **100/100 passed**
- Backend knowledge tests: **49 passed**
- Frontend personalization contract: **passed**
- TypeScript: **passed**
- Next.js production build: **passed**
- Mobile widths checked: **320 / 390 / 430 px**, no horizontal overflow

## Safety

Secrets and runtime data are intentionally excluded. Do not commit:

- `.env*`, API/OAuth/SMTP keys, credentials
- databases, Redis/PostgreSQL data
- model weights, uploads, user conversations or profiles
- build artifacts, logs, caches and dependency directories

The files in `recovery/` are idempotent migration scripts and can be reviewed before applying to a restored server copy. Runtime secrets, user data, databases, model weights and generated build/training artifacts are intentionally not stored in this public repository.
