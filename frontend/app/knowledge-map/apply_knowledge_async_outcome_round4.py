from __future__ import annotations

import sys
from pathlib import Path


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: apply_knowledge_async_outcome_round4.py PIPELINE")
    path = Path(sys.argv[1])
    source = path.read_text(encoding="utf-8")
    old = '''        except KnowledgeQueryUnavailable:
            raise
        except (AdmissionFull, AdmissionTimeout) as exc:
'''
    new = '''        except KnowledgeQueryNotResolved:
            # A normal, structured product outcome must reach the API route.
            # It is not an infrastructure failure and must not be wrapped as 503.
            raise
        except KnowledgeQueryUnavailable:
            raise
        except (AdmissionFull, AdmissionTimeout) as exc:
'''
    if new in source:
        print("async structured outcome already preserved")
        return
    if source.count(old) != 1:
        raise RuntimeError(f"expected one async exception block, found {source.count(old)}")
    path.write_text(source.replace(old, new, 1), encoding="utf-8")
    print("async structured outcome propagation patched")


if __name__ == "__main__":
    main()
