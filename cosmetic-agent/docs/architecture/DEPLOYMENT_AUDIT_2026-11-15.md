# Deployment and code audit — 2026-11-15

> Historical baseline. See `PLATFORM_DELIVERY_PLAN.md` for current evidence:
> Redis and the Mac database are now provisioned, model recovery was partially
> verified through the user's SSH outputs, and isolated worker benchmarks have
> been added. Missing-setting statements and subjective CIA percentages below
> describe the initial inspection, not the latest readiness state.

## Executive status

The repository is aligned to a Mac frontend/backend plus SSH-forwarded MI300X model topology. Code-level controls and offline tests can be completed locally; live deployment remains **incomplete** until the real SSH username, Mac `.env`, persistent remote artifacts, and external services pass `ops/runtime/verify_platform.sh`.

No large dead-code deletion was performed.

## Active production inventory

| Owner | Model or service | Current source/version | Runtime use | Status |
|---|---|---|---|---|
| External API | Terra-compatible OpenAI model | `gpt-5.6-terra` | intent, knowledge, response composition, skin extraction, product copy | Configured; `OPENAI_API_KEY` missing locally |
| External API | OpenAI web model | `gpt-5.5` | product web search | Configured; live API not tested |
| MI300X | TAIDE | `taide/Gemma-3-TAIDE-12b-Chat` at `abae28ce090549c694cd130d8fe64a54a8f13a6e` | chat/title/report via `TAIDE_BASE_URL` | Production selection; remote files unverified |
| MI300X | BGE-M3 | `BAAI/bge-m3` at `5617a9f61b028005a4858fdac845db406aefb181` | GraphRAG embeddings via `EMBEDDING_BASE_URL` | Production selection; remote files unverified |
| MI300X, optional | BGE reranker v2 M3 | `BAAI/bge-reranker-v2-m3` at `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e` | GraphRAG reranking via `RERANKER_BASE_URL` | Disabled by default |
| MI300X, experimental | Qwen3-VL 4B Instruct | `Qwen/Qwen3-VL-4B-Instruct` at `ebb281ec70b05090aa6165b016eac8ec08e71b17` | photo analysis via `PHOTO_MODEL_BASE_URL` | Base weights absent here; remote presence unverified |
| External data | Neo4j | environment configured | GraphRAG and legacy identity reads | `.env` absent; unverified |
| External data | Zep Cloud | environment configured | conversation memory | `.env` absent; unverified |
| Mac data | PostgreSQL and Redis | environment configured | auth, usage, admission, rate limits, jobs | `.env` absent; unverified |

The canonical official-weight record is `ops/runtime/models.manifest.json`. Known SHA-256 values are recorded for BGE-M3, reranker, and both Qwen shards. Hugging Face unauthenticated metadata does not expose gated TAIDE LFS hashes, so the downloader records local SHA-256 values at download time; this is not represented as official checksum recovery.

## Checkpoints and missing artifacts

- No `*.safetensors`, PyTorch weight, or checkpoint file exists in this checkout.
- Photo experiment markers refer to `/var/lib/jenkins/tomod/experiments/skin-vlm/model`, but that directory and its weights are absent here.
- `research/adapter-export.json` refers to `BLINK_Passed_Research_Adapter.zip`; that archive is absent.
- Therefore the Qwen base model and self-trained photo LoRA are **not restored**.
- Self-trained checkpoints belong under `/mnt/goodagents/checkpoints`, then are copied with checksums by `backup-trained-checkpoint.sh`; they are not mixed with official model directories.

## Canonical and historical launch paths

Canonical production scripts:

- `ops/runtime/start-mi300x-models.sh`: model services only, remote loopback bind.
- `ops/runtime/start-ssh-tunnel.sh`: Mac loopback forwards after host attestation.
- `ops/runtime/start-mac-production.sh`: Mac router, FastAPI backend, and Next.js frontend.
- `ops/runtime/start-production.sh`: role dispatcher requiring `BLINK_RUNTIME_ROLE=mac|mi300x`.

Historical or experimental, not canonical production:

- Root `start_all.sh`, `restart_taide_servers.sh`, `restart_api*.sh`, and old team scripts.
- Root `taide_server.py`, `taide_fix_server.py`, `taide_server_graphrag.py`, `taide_server_skin.py`, and `taide_server_skin_tracking.py` Transformers-era servers.
- Old 8003 compatibility gateway, 8004 skin/report path, 8011 development backend, TAIDE A/B model `Gemma-3-TAIDE-12b-Chat-2602`, and `ops/sglang-green` experiments.
- `experiments/skin-vlm` training, benchmark, staged, patch, and report artifacts.

These remain for traceability and possible experiments. They were not bulk-deleted.

## Dependency and performance findings

Completed:

- Backend runtime is Python 3.12 with hash-locked `requirements-full.lock`.
- FastAPI/Starlette moved to maintained versions and missing `email-validator` was added.
- Frontend uses exact Next.js and Sentry versions; Sentry v11 telemetry collection is explicitly restricted.
- SSE admission waits before expensive profile/history/model setup, releases on setup failure or unconsumed responses, and holds capacity until a disconnected synchronous producer finishes.
- Model download is opt-in by key instead of downloading every candidate.
- Backend entry paths no longer inject the obsolete `/var/lib/jenkins/tomod/cosmetic-agent` path on Mac.

Dead-code and maintenance candidates retained for later approved cleanup:

- `cosmetic-agent/untitled.py` is empty.
- Version-named marker files such as `experiments/skin-vlm/=0.10` look like shell redirection residue.
- Several copied `*.patch`, `remote-*`, staged benchmarks, and root legacy launch/model servers are outside the canonical runtime path.
- A 90%-confidence Vulture scan also reported production candidates: `include_more` in the root orchestrator, `RECORD_STATUS_LABELS` in skin tracking, `Decimal` in usage tracking, two URL-split tuple members in retrieval health, and two callback parameters in `user_analysis_context_bridge`. They need call-site review because framework callbacks and unpacked values can be false positives.
- Old documentation describes all-in-one GPU topology and ports 8003/8004/8011; treat it as historical where it conflicts with `ops/runtime/SPLIT_DEPLOYMENT.md`.

Further speed work should follow production traces after this split deployment is live. The largest expected latency is external/model inference, not the small local client wrappers; speculative rewrites before measurement add risk.

## CIA completion assessment

These percentages measure implemented controls, not proof that a live deployment is healthy.

| Dimension | Completion | Evidence | Remaining gap |
|---|---:|---|---|
| Confidentiality | 75% | Model ports bind loopback; SSH host-key checking; clients reject public model endpoints; secrets stay in `.env`; Sentry data categories disabled; photo payloads are temporary and not logged | SSH account/host identity unauthenticated; `.env` secrets absent; school account policy/key rotation unverified |
| Integrity | 72% | Exact upstream revisions; available official SHA-256 values; download-time hashes; fail-closed mount proof; separate checkpoint checksums; hash-locked Python dependencies | TAIDE official LFS hashes unavailable; remote artifacts/LoRA unverified; same-volume backup is not disaster recovery |
| Availability | 55% | Persistent-path policy; health/readiness checks; admission cleanup; SSH keepalive; reproducible rebuild/post-rebuild verifier | No live end-to-end pass; no confirmed SSH username; no verified weights or external DB/API; no HA/independent backup |

Combined control maturity is approximately **67%**. Actual service readiness is **incomplete**, because the live checks still require the school SSH credential and runtime configuration.

## Acceptance matrix

| Acceptance item | Current status |
|---|---|
| Local website and backend | 未完成 — `.env` absent and services not started |
| MI300X SSH identity | 未完成 — Jupyter URL known and `140.119.24.206:30892` returns SSH keys; username, independent fingerprint confirmation, and GPU attestation missing |
| Remote TAIDE API | 未完成 — tunnel, service, and weights unverified |
| Remote embedding API | 未完成 — tunnel, service, and weights unverified |
| GraphRAG | 未完成 — Neo4j and end-to-end query unverified |
| Photo analysis | 未完成 — disabled and required model artifacts absent/unverified |
| Model persistence after rebuild | 未完成 — rerun remote checksums after an actual rebuild |
| Offline backend/frontend security and regression checks | Completed locally; exact evidence appears in the handoff summary |
