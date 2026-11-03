# BLINK AI — recovery patches

This repository currently stores the sanitized recovery patches used to restore and harden the BLINK AI knowledge-map service.

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

The files in `recovery/` are idempotent migration scripts and can be reviewed before applying to a restored server copy.
