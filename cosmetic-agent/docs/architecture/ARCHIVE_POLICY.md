# Dead-code and artifact archive policy

## Principle

Deletion requires proof; archiving requires classification. Source that might be
imported, launched, migrated or used by a test is not dead merely because its name
looks old.

## Automatically archivable

- `*.bak*`, `*.old`, `*.orig`, `*.save`
- files inside `.ipynb_checkpoints/`
- top-level `codex_*.patch`
- top-level `gpucore.*` crash dumps
- the old top-level `cosmetic-agent-backup.tar.gz`
- `.pytest_cache/` and `.codex-security-backups/` generated/backups

These patterns are not valid production module paths. Before moving them, the
archiver verifies that no running process command line references the file.

## Never automatic

- normal `.py`, SQL migration, configuration or test files
- any active process entry point
- either duplicate repository tree
- dated docs whose operational value has not been reviewed
- model weights, product data or database exports

## Archive location

`/var/lib/jenkins/tomod/ops/archive/dead-code-20260925/`

Every moved file retains its project-relative path under `files/`. `MANIFEST.tsv`
records category, tracked state, bytes, SHA-256, original path and archive path.
Use the generated `restore.sh` to restore the whole archive or one relative path.
No archived artifact should be imported or used as architecture evidence.
