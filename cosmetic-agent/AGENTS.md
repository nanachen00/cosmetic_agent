# BLINK repository instructions

These rules apply to the entire `cosmetic-agent` repository.

1. Read `ARCHITECTURE.md` and `docs/architecture/README.md` before structural work.
2. Treat `api/api_server.py` as the current compatibility ASGI entry point. Do
   not change `uvicorn api.api_server:app` until the staged migration checklist
   has passed.
3. Use only `cosmetic-agent/.env` for Backend/Auth configuration. Never restore
   `backend-8010.env`, `auth-8011.env`, or a separate 8011 service.
4. The only production control scripts are under
   `/var/lib/jenkins/tomod/ops/runtime/`. Do not start model/API processes with
   ad-hoc commands.
5. Port 8200 is experimental and unmanaged. Production currently uses the
   managed 8000/8001/8002/8003/8010 topology.
6. Never create source-adjacent `.bak*`, `.old`, `.orig`, `.save`, `codex_*.patch`,
   or Notebook checkpoint files. Use Git for changes; use
   `/var/lib/jenkins/tomod/ops/archive/` only for recoverable operational archives.
7. Ignore archived and generated artifacts when searching or reasoning about the
   current implementation.
8. Before moving a module, search all imports and runtime launch scripts. Keep a
   compatibility import until callers and tests have migrated.
9. Prefer the existing structure instead of creating a parallel `backend/` tree:
   - routes: `app/api/routes/`
   - services: `app/services/`
   - clients: `app/clients/`
   - repositories: `database/postgres/repositories/`
   - shared runtime: `core/`
10. After backend changes run syntax/tests appropriate to the change and
    `/var/lib/jenkins/tomod/ops/runtime/status-production.sh`. A successful status
    must show `/readyz` and version alignment as OK.
11. Never expose `.env`, OAuth secrets, database credentials, Redis credentials,
    or API keys in logs, documentation, or command output.
