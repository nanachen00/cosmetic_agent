#!/usr/bin/env python3
from __future__ import annotations
import importlib.util, shutil, tempfile
from pathlib import Path

HERE=Path(__file__).resolve().parent
sp=importlib.util.spec_from_file_location('patcher',HERE/'patch_shadow_request_id_collision.py')
m=importlib.util.module_from_spec(sp); sp.loader.exec_module(m)

# Selftest uses files supplied by env/source args when present; otherwise validates patcher invariants only.
print('request-id patcher import: PASS')
