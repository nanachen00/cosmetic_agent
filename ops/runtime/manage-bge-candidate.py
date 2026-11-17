#!/usr/bin/env python3
"""Compatibility entrypoint restricted to the isolated 8027 BGE candidate."""
from pathlib import Path
import runpy
import sys

if __name__ == '__main__':
    if any(value == '--instance' or value.startswith('--instance=') for value in sys.argv[1:]):
        raise SystemExit('Candidate entrypoint cannot select another instance')
    target = runpy.run_path(str(Path(__file__).with_name('manage-bge-model.py')))
    raise SystemExit(target['main']([*sys.argv[1:], '--instance', 'bge-model-candidate']))
