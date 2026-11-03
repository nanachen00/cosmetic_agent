# Dead-code archive report

Executed: **2026-09-25**  
Archive: `/var/lib/jenkins/tomod/ops/archive/dead-code-20260925`

This cleanup moved **332** non-runtime artifacts (1183180529 bytes) out of
the active repository. No normal Python module, SQL migration, test, data file,
model weight, environment file, or running process entry point was moved.

| Category | Files |
|---|---:|
| codex-patch | 8 |
| gpu-core-dump | 1 |
| jupyter-checkpoint | 260 |
| old-project-bundle | 1 |
| pytest-cache | 5 |
| security-backup | 2 |
| source-snapshot | 55 |

The external archive contains `MANIFEST.tsv` with SHA-256 hashes and an
executable `restore.sh`. Git-tracked snapshots remain recoverable both from Git
and from the operational archive. See `ARCHIVE_POLICY.md` for classification.
