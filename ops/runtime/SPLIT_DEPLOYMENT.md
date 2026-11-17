# Mac + MI300X split deployment

This is the canonical production topology as of 2026-11-15.

- Mac: Next.js frontend, FastAPI backend, Terra text clients, and data-service clients.
- School MI300X: BGE-M3 and explicitly enabled GPU services such as Qwen photo analysis. TAIDE/router is a disabled-by-default legacy option.
- Transport: model APIs bind to remote `127.0.0.1` and reach the Mac only through SSH local forwarding.
- Persistence: every model, Hugging Face cache, checkpoint, backup, and runtime output lives below the verified `/mnt/goodagents` mount.
- The restored ROCm environment defaults to `/mnt/goodagents/blink-runtime/envs/vllm-rocm-v092`. Because vLLM and embedding currently share it, dependency setup validates it without mutation unless `ALLOW_SHARED_MODEL_ENV_MUTATION=1` is explicitly supplied.

The known JupyterLab URL is `http://140.119.24.206:31484/lab`. It is not an SSH endpoint. Authenticated SSH as `root@140.119.24.206:30892` has now verified the actual MI300X and the school NFS mount. The Jupyter port is not used for SSH.

## 1. MI300X preparation

On the actual GPU host, deploy this repository and run:

```bash
cd /absolute/path/to/cosmetic_agent
ops/runtime/verify-mi300x-host.sh
ops/runtime/verify-model-artifacts.py --models taide-prod,embedding
```

The school instance has already restored TAIDE at
`/mnt/goodagents/blink-runtime/data/models/taide-12b` and BGE-M3 in its persistent
HF cache. Audit and register/link these existing artifacts before bootstrap;
do not redownload them merely because they are not yet at the new manifest path.
Do not start a second model process while the restored service occupies its port.
`install_all.sh` is for rebuilding missing environments/artifacts after that
inventory; the old vLLM 0.8.5 compiler patch is a manual repair, never a routine
step for the working vLLM 0.9.2 environment.

The default download set contains only the production TAIDE and BGE-M3 models. Add a model only when its service is enabled, for example:

```bash
REQUESTED_MODELS=taide-prod,embedding,reranker ops/runtime/install/50_download_models.sh
```

`verify-mi300x-mount.sh` refuses to create `/mnt/goodagents`. The directory must already be an active mount point; only after that proof may the script create:

```text
/mnt/goodagents/models
/mnt/goodagents/hf-cache
/mnt/goodagents/checkpoints
/mnt/goodagents/checkpoint-backups
/mnt/goodagents/outputs
```

Downloads write `.blink-artifact.json` with the repository revision and download-time SHA-256 values. `verify-model-artifacts.py` never downloads or repairs files.

For a model that already exists on the persistent volume, first place or link it
at its canonical manifest path, then register it without downloading:

```bash
python3 ops/runtime/register-existing-model-artifact.py \
  --model taide-prod \
  --observed-revision abae28ce090549c694cd130d8fe64a54a8f13a6e
```

Registration refuses revision or known official checksum mismatches and will not
replace existing metadata.

Back up a self-trained checkpoint separately from official weights:

```bash
ops/runtime/backup-trained-checkpoint.sh /mnt/goodagents/checkpoints/<run-name> <backup-label>
```

The default backup protects against accidental replacement, not loss of the whole volume. An independent-storage copy is still required for disaster recovery.

## 2. Prove the SSH target

On the Mac, use the real school account. Do not substitute the Jupyter URL:

```bash
export MI300X_SSH_HOST='<school-user>@140.119.24.206'
export MI300X_SSH_PORT=30892
ops/runtime/verify-mi300x-ssh.sh
```

This check requires Linux, `/dev/kfd`, MI300X or `gfx942` output, and a real `/mnt/goodagents` mount. Host-key verification is mandatory.

The fingerprints observed without authentication on 2026-11-15 were:

```text
ECDSA   SHA256:Q4vM4sjs2hbqU3Vn935IjsBwataoRhLUoaA9AEzgXlI
RSA     SHA256:kz7utaTlFAhE0e4DZT4ZaEc/2ByfA8bhQgbV92EzCMA
ED25519 SHA256:ZfAmdiRfwtE5B/ZIOaTrKKlaDaNdIIudopmVbmUpCzU
```

Confirm one fingerprint through the school administrator before trusting it; observing it over the same network is not independent identity proof.

Start the foreground tunnel in its own terminal:

```bash
ops/runtime/start-ssh-tunnel.sh
```

The tunnel reads the feature flags from the single `cosmetic-agent/.env` and forwards embedding 8002 by default. TAIDE 8000 is forwarded only with `ENABLE_TAIDE=1`; reranker 8006 and photo 8025 are forwarded only when enabled. Internal vLLM ports 8200/8201 are never exposed by this tunnel.

Set `ENABLE_TAIDE=0` for the Terra deployment. Mac startup and `/readyz` then require neither TAIDE nor router8001; embedding remains independent. `terra_config` in readiness checks only that the API key is configured and does not claim external inference availability. Intent, input guard, pain/gain, product experience and response composition use `TERRA_MODEL` through Responses; old `OPENAI_INTENT_MODEL` and `RESPONSE_COMPOSER_MODEL` values are ignored. Product experience preserves its JSON-object format and 700-token output limit, uses the shared `TERRA_TIMEOUT_SECONDS` (default 20 seconds), and sends no automatic retries. Keep `CONVERSATION_TITLE_LLM=0` to use deterministic titles without API calls. No runtime stop or model deletion is performed by changing these flags.

## 3. Mac reproducible install and startup

```bash
cd cosmetic-agent
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python --require-hashes -r requirements-full.lock
cp .env.example .env
# Fill .env locally; never commit it.

cd ../frontend
npm ci
cd ..
ops/runtime/build-mac-frontend.sh
BLINK_RUNTIME_ROLE=mac ops/runtime/start-production.sh
```

Model endpoints stay environment-driven and must resolve to Mac loopback addresses. `start-mac-production.sh` rejects public remote model URLs.

## 4. Acceptance and rebuild proof

Set the remote repository path and run the read-only suite from the Mac:

```bash
export MI300X_CODE_ROOT=/absolute/path/to/cosmetic_agent
ops/runtime/verify_platform.sh
```

It independently checks the local website/backend, actual TAIDE generation, a 1024-dimensional BGE-M3 embedding, Neo4j plus the GraphRAG query endpoint, an actual photo sample when enabled, and remote model metadata/checksums. Run the same command after the school instance is rebuilt; a failed artifact check means the weights are not restored.
