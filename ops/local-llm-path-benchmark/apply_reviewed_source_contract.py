#!/usr/bin/env python3
"""Update reviewed Shadow source metadata only; never patch/restart services.

Default: --check-only. --apply updates exactly source_contract.json and
MANIFEST.sha256 under ops/local-llm-path-benchmark after rechecking live hashes.
No HTTP, subprocess, model, account-file, or process-environment access.
"""
from __future__ import annotations
import argparse
import ast
import base64
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tempfile
import uuid

# Filled from the reviewed member-harness delivery and the supplied source delta.
BASE_FILES = {'source_contract.json': {'sha256': '11cf62252403ac24faf81b56107bf0bf078b965f0a31fb216b4133e552a317b9', 'base64': 'ewogICJzbmFwc2hvdCI6ICJibGluay1zZXJ2ZXItc3RhdGUtMjAyNjA5MjQtMjIzNDQ1IiwKICAic25hcHNob3RfYXJjaGl2ZV9zaGEyNTYiOiAiZmQ2YTViMzUxOGY3ODc1ZjEwNGJkNzUxMDg2YTc4MDg1ZDk1MjdlMDdiNGY3YTEwMWIwZWQ5OWE5ZDYzNGE3YSIsCiAgImNyaXRpY2FsX29yaWdpbmFsX3NoYTI1NiI6IHsKICAgICJhZ2VudHMva25vd2xlZGdlL2tub3dsZWRnZV9zdWJfYWdlbnQucHkiOiAiZWE1MmVmNWMwZjU1ZjVhZjUzMTJhMWVhNDQ0NmUzODQ4M2U0ODQ1NGU1NTI2ZmQ4N2I5YTk3NmFkYjcwMmFhMiIsCiAgICAiYWdlbnRzL3Byb2R1Y3QvcHJvZHVjdF9hZ2VudC5weSI6ICJmNDI1NzY1NWJlNTRjNzczNmNkYjc2ODU5ODU4MThjMzUyZjczZjg1OTMyZGVjNzAyNzVlYTI5MDMyM2Q3OWI2IiwKICAgICJhZ2VudHMvcm9vdC9vcmNoZXN0cmF0b3IucHkiOiAiODZjZGZmOGY2OWZkY2ZiODVkNDMwMWIzMGE4YmQwZDhmNTgyNDRjZjkzMWIwZDJkY2E4ZDUzNTIzYWUzYjlkYyIsCiAgICAiYXBpL2FwaV9zZXJ2ZXIucHkiOiAiZTQ1YmZhMzE4NzVjZmZlZjgzMDc3MmUxYjFkZWNkMGM2ZDRhNDFjNmJhMjEzYzU5YjIxY2U5MDNiYWUxOWRlNyIsCiAgICAiYXBwL2NsaWVudHMvY2hhdF9jb21wbGV0aW9uX2NsaWVudC5weSI6ICI3YmMzZjhjYzJlZmMzZjQyM2FjYWM4MGVhYWU5ZGFjYjkzNmU0MDA1YjZhMzNiOTVhNTFhNDRiOTMwZDc1NzkzIiwKICAgICJhcHAvY2xpZW50cy9lbWJlZGRpbmdfY2xpZW50LnB5IjogIjVjN2U1OTIyMjMxZWUwMjkxMjk5OWM0NGEzYjUwYmU1YjJmYjExNWU5NmZjODFlODQzMjFlMTM2MzU5ZDg1MTQiLAogICAgImFwcC9jbGllbnRzL2tub3dsZWRnZV9yZXNwb25zZV9jbGllbnQucHkiOiAiZmRjYWViYjliZDc2MjM1YjgwMjViNDA0M2ExNmI2ZGE4ODU0ZTYwYzhhZjBjYzZiMzUxOTg1ZjUxNmQxZTZkZiIsCiAgICAiYXBwL3NlY3VyaXR5L2NoYXRfZ3VhcmQucHkiOiAiMDhiOWM2OTQ3ZGIzM2VkM2I5ZjA2NzczNGNlYTBhOTlkZGZiMGQwMTg3OTczMmZhMTI2OTM5YWIxMzJjNDQxMCIsCiAgICAiYXBwL3NlY3VyaXR5L3JlcXVlc3RfZ3VhcmQucHkiOiAiZDNhNzYzMGQwNGJkMjA1YTNjMjM0YzU4NjczMDg4N2Y0NzZlZGZlNGViYjcwMmNlNzlhOTljZjYzYjJmMmJjMiIsCiAgICAiY29yZS9hZG1pc3Npb24ucHkiOiAiZWEwOWJjYTBiYTBlY2EyMDVlYjEzZjcxOGNjMWQ0NGY3ZjE0YWJlN2FjYmEwMjVmZTE2MGE0MjkzNTRhMWUzYSIsCiAgICAiY29yZS9rbm93bGVkZ2VfcmVzcG9uc2VfY29uZmlnLnB5IjogIjQxMDQwY2VlNGM2YWFmYTYxYWMyNjZiMDVhN2M5MWFmOWUxNGI1YmM1MWYxYmVlOGMwY2FmNmVkMjgyMGE4YWIiLAogICAgImNvcmUvb2JzZXJ2YWJpbGl0eS5weSI6ICIxNzNlMDQ4MjYzNThjZmFhNjBjZTc3MmQ5MmZmMjc1YmJkZGViNTczMzk4ZTYyMjg0NmYwNmYzMmJjNzQ0YTdiIiwKICAgICJjb3JlL3VzYWdlX3RyYWNraW5nLnB5IjogImE3YWFiYTg2MGNjYmU3Yzk5OTYwNmE0M2I5MTgxMDdlYzQyYjkzYjM3ODExODQ0NzVlZjYxYzQwYTBmOTcyNDciLAogICAgInRvb2xzL2FnZW50L2NvcHl3cml0aW5nX3Rvb2wucHkiOiAiYTZmMjdlODNkMmMxMDExMDI1NzY0NDZmNzAwODVkY2M5YjIwODZhMWM5NzNhM2FkYWI5M2M3OGE1ZDYxMWRlZSIsCiAgICAidG9vbHMvYWdlbnQvaW5wdXRfZ3VhcmQucHkiOiAiMGVkYTU5ZDVmZTRmZGZhMzE0NjlmNGM1NTBlMmFmYzFjMGRiOTVmYjI3NjQwZWU3MjdlYWEzYTE1YWY0YTI1MSIsCiAgICAidG9vbHMvYWdlbnQvaW50ZW50X3BhcnNlci5weSI6ICJkYTZmNjIzODFlNzhhMzgxYmNjNDZiNDFhNGE4ZDY4NDExNWU0NmY0OGQzZDE4NDUwZjkyYjVmOTAyMGVhYjEwIiwKICAgICJ0b29scy9zZWFyY2gvZ3JhcGhfc2VhcmNoLnB5IjogImZiYmUzNDcyM2NjNzdiNzlmY2NlZTQyZjZiYWJkNjM1YmZiOTA2Yzc5N2YzODI2ZjdlMTcxMDFhNWNiN2JjMGMiLAogICAgInRvb2xzL3NlYXJjaC9pbmdyZWRpZW50X2V4dHJhY3Rvci5weSI6ICJmYzI0NDdmMTU0YTU5Y2E0OTc1ZWU4ZmI3NGZiYzcxNjgyMWM2YWZmMjA3ZTgzYzJjNmNlM2YzMjE5ZjBlMjI3IiwKICAgICJ0b29scy9zZWFyY2gva25vd2xlZGdlX2ludGVudC5weSI6ICIwZjQxODY5NTVlOTRhN2YzNDBhMWMxMTUzNWQxNTE5ZDQwZDE1NmZhM2NmYzA2YmE1MWNjZGZmNDQzZWFiYmYyIiwKICAgICJhcHAvYXBpL3JvdXRlcy9hdXRoLnB5IjogIjczNTc5YjEzMzc2YzJkYTY3NDJkY2FlYjFkOWEzN2ZkZGRlOGE4ZGIzZDIxMTE4MDY2MWNmMzNjYjdmMDExNmEiLAogICAgImFwcC9hcGkvc2NoZW1hcy9hdXRoLnB5IjogImI3N2ExYmIzZmYyYzY4N2E0YWM5ZjkxMmQ2MmEzYjg5YWMzMDhhMmI1YjVkNjhhMTYwZjRlN2RmNjc5YjczOGMiLAogICAgInRvb2xzL2F1dGgvYXV0aF9zZXNzaW9uLnB5IjogIjQ5NjE0Mjg0ODFmNDcxZTc3MDk5MjQzMmZjMzhkNzBmYTU3OWQ3ZGI4ODg1OWI3ZTJkOThhZTY2NmQ2YWM1OGQiLAogICAgInRvb2xzL2F1dGgvcG9zdGdyZXNfYXV0aF9zZXNzaW9uLnB5IjogIjFkYTQxNjA5MjU0ODlkMTQyOTA2MTYzMGQyNDhhNzM4ZDJlNWQ1ZTI5ZWNjZDRiOTFkZjRlNGMwZjRjY2U3MGUiCiAgfSwKICAibm90ZSI6ICJTbmFwc2hvdCBzb3VyY2UgY29udGFpbnMgcmVkYWN0aW9ucy4gUmVhZCBsaXZlIHNlcnZlciBieXRlcywgdmVyaWZ5IHRoZXNlIG9yaWdpbmFsIGhhc2hlcywgZG8gbm90IGRlcGxveSBzbmFwc2hvdCB0ZXh0LiIKfQo='}, 'MANIFEST.sha256': {'sha256': 'a828bb95b9f98b73c9b020893ea9455aa118bcd740e31f7e3a753d8167b1c25c', 'base64': 'MzA5ZThmNTU5NmY1ODQwODUzNmZhYmU0MTVjMzJkOWIzZjA3M2ZmYzdmYzU5ZTRjYWNhOWU2MGZkYWMyNDcxOCAgUkVBRE1FLm1kCmQ2YjYwOTViNmI5MTBiYTI2MDEzNzk1ZWIyNTI1MWY3NjE4ZGQ2ZDVjZjc4ZjM5OTA3MGZjOGM4MmMxODY5Y2EgIFNNT0tFX1JFVklFVy5tZAoxYjA2YzQ0NmJhZWVhNmU2ZTkxYTczMmU2OTg4YTk2ZTgyNGU5OGY3YmNlY2EwNWJhMjdhMGU0OTIxYTI4ZjEyICBTT1VSQ0VfTk9URVMubWQKYjMwZmY4MjNmNGVlYTlkZTFmMTVhMTEzMDk1OGY2MmMyZGNiYWNhZDNhMjdhZDA5YmI3NDQ1YjRjZWU4YzVjMiAgVEVTVF9SRVBPUlQubWQKM2YyNTRmNGJlMTM1Y2NjMjhmM2JmYWQzOTA3MWI4NzQ2MjYxN2NiZDIwMTRhMWMyZTE3Y2RhZjUwMmIwNzBkNiAgYWNjb3VudHMuZXhhbXBsZS5qc29uCjY0MzYxOGVjYmRmZjUwZWQzNTlkY2RlMjYxNTUxOTIwMDBlZjYyYzk1NDkyMjkzM2E2ZTNlODM1MWNlMTc3ZTkgIGF1dGhfY2xpZW50cy5weQoxZWUwZWI2ZDBkMzA1MzliMjU5ZTgzZmVmNTM2MzBjN2IyYzdlMWJjMmU4NDA0NWU2YWUyYTczM2MwNzVmZDAzICBiZW5jaC5weQo0OGYxZDI4MGJhMzllNWZkOGQxMDI5YWU1MmU4YzdhOGEyMzgwMmU4NmMyNjVkNWRlOWU2YmNjYmRmOTc2OGZkICBjb21tb24ucHkKMmQwOWQ0NGNkMjZmOTJiNmNlNGM5MWMzYmUxMTg5NDljMzg4MTVkMDhhNGUwZThlMWNlYzcyYzZmMjZiYzVmYSAgY29tcGF0X2dhdGV3YXkucHkKMzY4NjlkMTQzODg5NTYwMzQyYzU4NTAxMjZmY2Q5MWVjNTYwZGE2YzkyZjMxMGQ4ZGZmNTc5YzA2Y2M1OWFlZiAgY29uZmlnLmpzb24KNTQwMGU2YmZiOGRhNDNhNzlmYzc4NWJkMDhkNzA2MzVkODgwOTU1MjNkNmY4NzI0NzU4OWEyMDVjOGIxZmNlNCAgY29uZmlnX3ByZXBhcmUucHkKZGU0OTlkYjI1ZGM4NGZhNDFlMjNiYmU3MWI4ZTA2NDhmYmQ4NmRmMmQxMTI1Y2QyMDljNTM2ODgyY2M0NGRmMSAgY29uZmlndXJlX3Rlc3RfYWNjb3VudHMucHkKOTk4NDY1OGZjNDhjMjNiNDVhMmM1ZGVmYWExYTg5YWYzOGU2NDA5NTZlY2Q2NTVjYzYwNTM0ZDEyOTczMTQ5MiAgcGFja19yZXN1bHRzLnNoCmM3ODg0NGYzZTBjMzI1MTQxMTM0YmVkNmQ0YTAyNTQxNDkzYTZhMzVjN2NjM2RjNzlmODM4NGRiYzNkODk4NDEgIHByb2Nlc3NfY29udHJvbC5weQo5M2Q5MTRlNWUxNzBlYjQ3YmU2ZjY3MzA5ODE1MzkzNGE0MTUxNGUxZDgyZTIyMmZlOTNhYzA2MGQ4YjE0MzQyICBwcm9maWxlX2JhY2tlbmQucHkKMzU0YTM5ODFlODBlNjQ0ZGY4YmI2ZTRmZDA3MTBhNGU3ZDBlOTkyNWUwYjEwNDllZWI2NGQxZGZmYjllYmY3NCAgcmVkaXNfbG9jYWwucHkKNzcwNTRjMmU3NTk2MjgwYWVhNzZjOTA3ODAxZThiNjdmMThhZDUzZjc2OTZiM2ZjNjcyOThiN2NjN2Y2ZDJkYyAgcmVzdG9yZV90ZXN0X3N0YXRlLnNoCmExYjNlODNmOGQ4ZDkxMWY2ZDYxZmExMDQwN2ExYWY3NTU0MWIxYTliMzE1YTdjNDc1YzBjZWFiZjYwMWU3OTYgIHJ1bl9sb2NhbF9wcm92aWRlcl9wYXRoX2JlbmNobWFyay5zaAplYTQyMzAxMmQzN2E1OWNlYjJjNjQ1NjdhYjZjYTA5YjUxOTQ2MTk0MzVhZWRjZGQwMWNlMjYwZGVlMTZiNzFjICBydW5fdmVyaWZpZWRfc2hhZG93X3Rlc3RzLnNoCjcxYTBmYjM2YTJkZDYzNzcxYTMwMzkzYjJmYTIyODdlZDI4NjVkOTZjNWYyNjBhNWRhNDU3ZjI1Mzc5M2EwM2YgIHNlbGZ0ZXN0LnNoCmRjY2VmYzYwM2ZhMjkwMGZlMTUxYTlmMDYzNDIxMmIwYTQzMDVmMGJmNGZiM2EzNTRkNGZmMzJhYmVhMTQ3ZWMgIHNoYWRvd19ib290c3RyYXAucHkKMGJjNTY4ZDZiN2I4NTM5NGI5NGU3ZjU4MmExNjhiZGE5MGY0YjE5NjBhMjQzMGYyYTA0MTczY2VjMDZkNDAzMyAgc2hhZG93X2NvcHkucHkKZGZjZmY4NjQ0OTQ4NTQzZTBjMzAxZDk0NWQwNTkxYmZkOTE3MzUxZDY5YWQ4YmEwNWU5YzFhMzZkMmU5ZDk5ZCAgc2hhZG93X2VudHJ5LnB5CjdhMDBmZTNlNmQ0NTNjN2YwZDg0NjU3Y2VlMmJhZWJhNTgyMzNmYzljMWNmMDAzZGJlYTE1YTVmMmUyZTk2YmIgIHNoYWRvd19ndWFyZC5weQo4NGNiYzAwYjkxOTg0ZTJlMGNkOTQ1NDU2YmJmZTRhYjUwZDFiNDZjMGY0YTBhMzBhMjYxYmVjOGZhZGMwZmIzICBzbW9rZV9jaGVja3MucHkKMTFjZjYyMjUyNDAzYWMyNGZhZjgxYjU2MTA3YmYwYmYwNzhiOTY1ZjBhMzFmYjIxNmI0MTMzZTU1MmEzMTdiOSAgc291cmNlX2NvbnRyYWN0Lmpzb24KZWE3ZDA2YzA1N2Q0MGJiZWRkNzE2ODZhNzVjYzQ3NmFlOWE3N2E5OWM0ZDM5MjU4YWI5YjcwZDJkODBiOGQwNyAgdGVsZW1ldHJ5LnB5CjkxZWU5MmYxODU4YTQ1NDBlYjBhYWM4MjFhMzA3OWI4MDE4MDg1ZTZmNDlkYTM2ZTY1YTdkZTYxYWFhOTQzYjggIHRlc3RzL3Rlc3RfYXV0aF93b3JrZmxvdy5weQoyMDkzYjZiMGMxY2U3MzJlZGZiNWQ0ZjFmNGExYWZlNmNhYjhjZjRiYTY1NmNjNDNkOWQzYTJjNzZjZjU1YzZkICB0ZXN0cy90ZXN0X2J1bmRsZS5weQo='}}
NEXT_FILES = {'source_contract.json': {'sha256': '9d2dfc5bff14293d0fa2f07c219b377e15c2e86a4e6303611816373456271a08', 'base64': 'ewogICJzbmFwc2hvdCI6ICJzb3VyY2UtZGVsdGEtMjAyNjA5MjUtMDk1MjAyLTU2MDU3MS0zYmQ4NzkiLAogICJzbmFwc2hvdF9hcmNoaXZlX3NoYTI1NiI6ICJiZDMxMDhlM2NkNmRjMDRkOTg0YzZhMGQ3YjFhOWQ3OGMyZTU5MWE0YmQ4ZTQ2ZjU4OGUzZjM5ZTZjNzI0MzZmIiwKICAiY3JpdGljYWxfb3JpZ2luYWxfc2hhMjU2IjogewogICAgImFnZW50cy9rbm93bGVkZ2Uva25vd2xlZGdlX3N1Yl9hZ2VudC5weSI6ICJlYTUyZWY1YzBmNTVmNWFmNTMxMmExZWE0NDQ2ZTM4NDgzZTQ4NDU0ZTU1MjZmZDg3YjlhOTc2YWRiNzAyYWEyIiwKICAgICJhZ2VudHMvcHJvZHVjdC9wcm9kdWN0X2FnZW50LnB5IjogImY0MjU3NjU1YmU1NGM3NzM2Y2RiNzY4NTk4NTgxOGMzNTJmNzNmODU5MzJkZWM3MDI3NWVhMjkwMzIzZDc5YjYiLAogICAgImFnZW50cy9yb290L29yY2hlc3RyYXRvci5weSI6ICJmZjM0YWQ3ZWQ1ZGQ2Zjc1NzE2NWIwNjc2NmMyMWMyYmQ0Y2M5OTdkM2E2NTIwMmRiZTNiZWM1ZjY1YTc3ZDA0IiwKICAgICJhZ2VudHMvc2tpbl90cmFja2luZy9leHRyYWN0aW9uLnB5IjogImUyNTUzZGRlMzc4NmIyZTQ0MzliODZhMzY1ZjhhMGY5MzlkZTUwYzE0Nzk4ZTQ5MzNjN2QwYTZiNTFmNWY2OTgiLAogICAgImFnZW50cy9za2luX3RyYWNraW5nL3RyYWNraW5nX2FnZW50LnB5IjogImJjYjI4YmQwYjVmZjg4M2JiNDhmMzhhYzk0YTYyMWU3NTIwNWRlYjNlZGVjNTlmYTkyYzJlOWRhNzUwMWE5ZTYiLAogICAgImFwaS9hcGlfc2VydmVyLnB5IjogIjZhYzIzNDFhNTY1YzAwMWExZjYxNTgwOWFhZDlmOWM4NzA1MzQ3N2IwZmU3YTEzM2YxNjY2ZmI1MDg0ZTg2ODciLAogICAgImFwcC9hcGkvcm91dGVzL2F1dGgucHkiOiAiNzM1NzliMTMzNzZjMmRhNjc0MmRjYWViMWQ5YTM3ZmRkZGU4YThkYjNkMjExMTgwNjYxY2YzM2NiN2YwMTE2YSIsCiAgICAiYXBwL2FwaS9yb3V0ZXMvZ3Vlc3Rfc2Vzc2lvbi5weSI6ICI2ODc2ZmM1NjI0Y2MzZGNkMThkYzg4NTRmYTg2YTY1ZmY2YWFiMjIwNjllNTQxNjk2ZDk1YWIxYmZiYzU5NjUwIiwKICAgICJhcHAvYXBpL3JvdXRlcy93ZWxjb21lLnB5IjogIjQ2YzRhYzU5MWUxOWVlZjhlMDU4MmNhODNlNjUzZDU0ODNiMjI0ODRhMjIyZTZkMWJlODkxZWQ0MWIxZGJlZGYiLAogICAgImFwcC9hcGkvc2NoZW1hcy9hdXRoLnB5IjogImI3N2ExYmIzZmYyYzY4N2E0YWM5ZjkxMmQ2MmEzYjg5YWMzMDhhMmI1YjVkNjhhMTYwZjRlN2RmNjc5YjczOGMiLAogICAgImFwcC9jbGllbnRzL2NoYXRfY29tcGxldGlvbl9jbGllbnQucHkiOiAiN2JjM2Y4Y2MyZWZjM2Y0MjNhY2FjODBlYWFlOWRhY2I5MzZlNDAwNWI2YTMzYjk1YTUxYTQ0YjkzMGQ3NTc5MyIsCiAgICAiYXBwL2NsaWVudHMvZW1iZWRkaW5nX2NsaWVudC5weSI6ICI1YzdlNTkyMjIzMWVlMDI5MTI5OTljNDRhM2I1MGJlNWIyZmIxMTVlOTZmYzgxZTg0MzIxZTEzNjM1OWQ4NTE0IiwKICAgICJhcHAvY2xpZW50cy9rbm93bGVkZ2VfcmVzcG9uc2VfY2xpZW50LnB5IjogImZkY2FlYmI5YmQ3NjIzNWI4MDI1YjQwNDNhMTZiNmRhODg1NGU2MGM4YWYwY2M2YjM1MTk4NWY1MTZkMWU2ZGYiLAogICAgImFwcC9zZWN1cml0eS9jaGF0X2d1YXJkLnB5IjogIjA4YjljNjk0N2RiMzNlZDNiOWYwNjc3MzRjZWEwYTk5ZGRmYjBkMDE4Nzk3MzJmYTEyNjkzOWFiMTMyYzQ0MTAiLAogICAgImFwcC9zZWN1cml0eS9yZXF1ZXN0X2d1YXJkLnB5IjogImQzYTc2MzBkMDRiZDIwNWEzYzIzNGM1ODY3MzA4ODdmNDc2ZWRmZTRlYmI3MDJjZTc5YTk5Y2Y2M2IyZjJiYzIiLAogICAgImFwcC9zZXJ2aWNlcy93ZWxjb21lX3NlcnZpY2UucHkiOiAiODY1MjE2MTExYzBjOWEwOGIzYTlmZGQyOTY4ZDFhNzJmZWQ4YzRkMjg3N2I2M2U1MjhlNTc3NzNkYzFmMjQzMSIsCiAgICAiY29yZS9hZG1pc3Npb24ucHkiOiAiZWEwOWJjYTBiYTBlY2EyMDVlYjEzZjcxOGNjMWQ0NGY3ZjE0YWJlN2FjYmEwMjVmZTE2MGE0MjkzNTRhMWUzYSIsCiAgICAiY29yZS9rbm93bGVkZ2VfcmVzcG9uc2VfY29uZmlnLnB5IjogIjQxMDQwY2VlNGM2YWFmYTYxYWMyNjZiMDVhN2M5MWFmOWUxNGI1YmM1MWYxYmVlOGMwY2FmNmVkMjgyMGE4YWIiLAogICAgImNvcmUvb2JzZXJ2YWJpbGl0eS5weSI6ICIxNzNlMDQ4MjYzNThjZmFhNjBjZTc3MmQ5MmZmMjc1YmJkZGViNTczMzk4ZTYyMjg0NmYwNmYzMmJjNzQ0YTdiIiwKICAgICJjb3JlL3VzYWdlX3RyYWNraW5nLnB5IjogImE3YWFiYTg2MGNjYmU3Yzk5OTYwNmE0M2I5MTgxMDdlYzQyYjkzYjM3ODExODQ0NzVlZjYxYzQwYTBmOTcyNDciLAogICAgInRvb2xzL2FnZW50L2NvcHl3cml0aW5nX3Rvb2wucHkiOiAiYTZmMjdlODNkMmMxMDExMDI1NzY0NDZmNzAwODVkY2M5YjIwODZhMWM5NzNhM2FkYWI5M2M3OGE1ZDYxMWRlZSIsCiAgICAidG9vbHMvYWdlbnQvaW5wdXRfZ3VhcmQucHkiOiAiMGVkYTU5ZDVmZTRmZGZhMzE0NjlmNGM1NTBlMmFmYzFjMGRiOTVmYjI3NjQwZWU3MjdlYWEzYTE1YWY0YTI1MSIsCiAgICAidG9vbHMvYWdlbnQvaW50ZW50X3BhcnNlci5weSI6ICJkYTZmNjIzODFlNzhhMzgxYmNjNDZiNDFhNGE4ZDY4NDExNWU0NmY0OGQzZDE4NDUwZjkyYjVmOTAyMGVhYjEwIiwKICAgICJ0b29scy9hZ2VudC90cmFja2luZy9za2luX3JlcG9ydF90b29sLnB5IjogImVmMjhkZDNhNjI3YWFjYWY2N2E5YjE5OWMwMTY2MGM0YWJkNzdlNjY1YTFjY2UyYmU0NzE3YjQ2YzJhMGZhNWIiLAogICAgInRvb2xzL2F1dGgvYXV0aF9zZXNzaW9uLnB5IjogIjQ5NjE0Mjg0ODFmNDcxZTc3MDk5MjQzMmZjMzhkNzBmYTU3OWQ3ZGI4ODg1OWI3ZTJkOThhZTY2NmQ2YWM1OGQiLAogICAgInRvb2xzL2F1dGgvcG9zdGdyZXNfYXV0aF9zZXNzaW9uLnB5IjogIjFkYTQxNjA5MjU0ODlkMTQyOTA2MTYzMGQyNDhhNzM4ZDJlNWQ1ZTI5ZWNjZDRiOTFkZjRlNGMwZjRjY2U3MGUiLAogICAgInRvb2xzL3NlYXJjaC9ncmFwaF9zZWFyY2gucHkiOiAiZmJiZTM0NzIzY2M3N2I3OWZjY2VlNDJmNmJhYmQ2MzViZmI5MDZjNzk3ZjM4MjZmN2UxNzEwMWE1Y2I3YmMwYyIsCiAgICAidG9vbHMvc2VhcmNoL2luZ3JlZGllbnRfZXh0cmFjdG9yLnB5IjogImZjMjQ0N2YxNTRhNTljYTQ5NzVlZThmYjc0ZmJjNzE2ODIxYzZhZmYyMDdlODNjMmM2Y2UzZjMyMTlmMGUyMjciLAogICAgInRvb2xzL3NlYXJjaC9rbm93bGVkZ2VfaW50ZW50LnB5IjogIjBmNDE4Njk1NWU5NGE3ZjM0MGExYzExNTM1ZDE1MTlkNDBkMTU2ZmEzY2ZjMDZiYTUxY2NkZmY0NDNlYWJiZjIiCiAgfSwKICAibm90ZSI6ICJMYXllcmVkIHJldmlld2VkIHNvdXJjZSBiYXNlbGluZTogdW5jaGFuZ2VkIGVudHJpZXMgcmV0YWluIHRoZSBiYXNlIHNuYXBzaG90IGhhc2hlczsgZWlnaHQgY29sbGVjdGVkIGZpbGVzIHVzZSB0aGlzIGRlbHRhIG9yaWdpbmFsIFNIQS0yNTYuIERvIG5vdCBkZXBsb3kgYW5hbHlzaXMgdGV4dC4gUHJlZmxpZ2h0IG11c3QgaGFzaCBsaXZlIHNvdXJjZS4gVGhpcyBtZXRhZGF0YSB1cGRhdGUgZG9lcyBub3QgY2hhbmdlIHByb2R1Y3Rpb24gY29kZSwgYXV0aCwgcm91dGluZywgZ3VhcmRzLCBtb2RlbCBzZXR0aW5ncyBvciBsb2FkZWQgcHJvY2Vzc2VzLiIsCiAgImJhc2Vfc25hcHNob3QiOiB7CiAgICAic25hcHNob3QiOiAiYmxpbmstc2VydmVyLXN0YXRlLTIwMjYwOTI0LTIyMzQ0NSIsCiAgICAic25hcHNob3RfYXJjaGl2ZV9zaGEyNTYiOiAiZmQ2YTViMzUxOGY3ODc1ZjEwNGJkNzUxMDg2YTc4MDg1ZDk1MjdlMDdiNGY3YTEwMWIwZWQ5OWE5ZDYzNGE3YSIKICB9LAogICJyZXZpZXdlZF9kZWx0YSI6IHsKICAgICJjb2xsZWN0aW9uX3N0YXJ0ZWRfYXQiOiAiMjAyNi0wOS0yNVQwOTo1MjowMi4yOTUrMDA6MDAiLAogICAgImNvbGxlY3Rpb25fZmluaXNoZWRfYXQiOiAiMjAyNi0wOS0yNVQwOTo1MjowMi41NjArMDA6MDAiLAogICAgImNvbGxlY3Rpb25fY29uc2lzdGVudCI6IHRydWUsCiAgICAiZXhpc3RpbmdfY29udHJhY3RfY2hlY2tlZCI6IDIzLAogICAgImV4aXN0aW5nX2NvbnRyYWN0X3VuY2hhbmdlZCI6IDIxLAogICAgInVwZGF0ZWRfZW50cmllcyI6IFsKICAgICAgImFnZW50cy9yb290L29yY2hlc3RyYXRvci5weSIsCiAgICAgICJhcGkvYXBpX3NlcnZlci5weSIKICAgIF0sCiAgICAiYWRkZWRfZW50cmllcyI6IFsKICAgICAgImFwcC9hcGkvcm91dGVzL2d1ZXN0X3Nlc3Npb24ucHkiLAogICAgICAiYXBwL2FwaS9yb3V0ZXMvd2VsY29tZS5weSIsCiAgICAgICJhcHAvc2VydmljZXMvd2VsY29tZV9zZXJ2aWNlLnB5IiwKICAgICAgImFnZW50cy9za2luX3RyYWNraW5nL3RyYWNraW5nX2FnZW50LnB5IiwKICAgICAgInRvb2xzL2FnZW50L3RyYWNraW5nL3NraW5fcmVwb3J0X3Rvb2wucHkiLAogICAgICAiYWdlbnRzL3NraW5fdHJhY2tpbmcvZXh0cmFjdGlvbi5weSIKICAgIF0sCiAgICAiZXhwb3J0ZWRfc291cmNlX2ZpbGVzIjogOCwKICAgICJzdGF0aWNfZGVwZW5kZW5jeV9jaGVja3MiOiA3OCwKICAgICJzdGF0aWNfZGVwZW5kZW5jeV9zY29wZSI6ICJvbmx5IHNlbGVjdGVkL25ldy9jaGFuZ2VkIFB5dGhvbiBBU1RzOyBzdG9wIGF0IHVuY2hhbmdlZCByZXZpZXdlZCBieXRlczsgZHluYW1pYyBpbXBvcnRzIG5vdCBleGVjdXRlZCIsCiAgICAib3JpZ2luYWxfc2hhMjU2X2V2aWRlbmNlIjogewogICAgICAiYWdlbnRzL3Jvb3Qvb3JjaGVzdHJhdG9yLnB5IjogewogICAgICAgICJiZWZvcmVfc2hhMjU2IjogIjg2Y2RmZjhmNjlmZGNmYjg1ZDQzMDFiMzBhOGJkMGQ4ZjU4MjQ0Y2Y5MzFiMGQyZGNhOGQ1MzUyM2FlM2I5ZGMiLAogICAgICAgICJyZXZpZXdlZF9zaGEyNTYiOiAiZmYzNGFkN2VkNWRkNmY3NTcxNjViMDY3NjZjMjFjMmJkNGNjOTk3ZDNhNjUyMDJkYmUzYmVjNWY2NWE3N2QwNCIsCiAgICAgICAgImFuYWx5c2lzX3NoYTI1NiI6ICJmZjM0YWQ3ZWQ1ZGQ2Zjc1NzE2NWIwNjc2NmMyMWMyYmQ0Y2M5OTdkM2E2NTIwMmRiZTNiZWM1ZjY1YTc3ZDA0IgogICAgICB9LAogICAgICAiYXBpL2FwaV9zZXJ2ZXIucHkiOiB7CiAgICAgICAgImJlZm9yZV9zaGEyNTYiOiAiZTQ1YmZhMzE4NzVjZmZlZjgzMDc3MmUxYjFkZWNkMGM2ZDRhNDFjNmJhMjEzYzU5YjIxY2U5MDNiYWUxOWRlNyIsCiAgICAgICAgInJldmlld2VkX3NoYTI1NiI6ICI2YWMyMzQxYTU2NWMwMDFhMWY2MTU4MDlhYWQ5ZjljODcwNTM0NzdiMGZlN2ExMzNmMTY2NmZiNTA4NGU4Njg3IiwKICAgICAgICAiYW5hbHlzaXNfc2hhMjU2IjogIjZhYzIzNDFhNTY1YzAwMWExZjYxNTgwOWFhZDlmOWM4NzA1MzQ3N2IwZmU3YTEzM2YxNjY2ZmI1MDg0ZTg2ODciCiAgICAgIH0sCiAgICAgICJhcHAvYXBpL3JvdXRlcy9ndWVzdF9zZXNzaW9uLnB5IjogewogICAgICAgICJiZWZvcmVfc2hhMjU2IjogbnVsbCwKICAgICAgICAicmV2aWV3ZWRfc2hhMjU2IjogIjY4NzZmYzU2MjRjYzNkY2QxOGRjODg1NGZhODZhNjVmZjZhYWIyMjA2OWU1NDE2OTZkOTVhYjFiZmJjNTk2NTAiLAogICAgICAgICJhbmFseXNpc19zaGEyNTYiOiAiNjg3NmZjNTYyNGNjM2RjZDE4ZGM4ODU0ZmE4NmE2NWZmNmFhYjIyMDY5ZTU0MTY5NmQ5NWFiMWJmYmM1OTY1MCIKICAgICAgfSwKICAgICAgImFwcC9hcGkvcm91dGVzL3dlbGNvbWUucHkiOiB7CiAgICAgICAgImJlZm9yZV9zaGEyNTYiOiBudWxsLAogICAgICAgICJyZXZpZXdlZF9zaGEyNTYiOiAiNDZjNGFjNTkxZTE5ZWVmOGUwNTgyY2E4M2U2NTNkNTQ4M2IyMjQ4NGEyMjJlNmQxYmU4OTFlZDQxYjFkYmVkZiIsCiAgICAgICAgImFuYWx5c2lzX3NoYTI1NiI6ICI0NmM0YWM1OTFlMTllZWY4ZTA1ODJjYTgzZTY1M2Q1NDgzYjIyNDg0YTIyMmU2ZDFiZTg5MWVkNDFiMWRiZWRmIgogICAgICB9LAogICAgICAiYXBwL3NlcnZpY2VzL3dlbGNvbWVfc2VydmljZS5weSI6IHsKICAgICAgICAiYmVmb3JlX3NoYTI1NiI6IG51bGwsCiAgICAgICAgInJldmlld2VkX3NoYTI1NiI6ICI4NjUyMTYxMTFjMGM5YTA4YjNhOWZkZDI5NjhkMWE3MmZlZDhjNGQyODc3YjYzZTUyOGU1Nzc3M2RjMWYyNDMxIiwKICAgICAgICAiYW5hbHlzaXNfc2hhMjU2IjogIjg2NTIxNjExMWMwYzlhMDhiM2E5ZmRkMjk2OGQxYTcyZmVkOGM0ZDI4NzdiNjNlNTI4ZTU3NzczZGMxZjI0MzEiCiAgICAgIH0sCiAgICAgICJhZ2VudHMvc2tpbl90cmFja2luZy90cmFja2luZ19hZ2VudC5weSI6IHsKICAgICAgICAiYmVmb3JlX3NoYTI1NiI6IG51bGwsCiAgICAgICAgInJldmlld2VkX3NoYTI1NiI6ICJiY2IyOGJkMGI1ZmY4ODNiYjQ4ZjM4YWM5NGE2MjFlNzUyMDVkZWIzZWRlYzU5ZmE5MmMyZTlkYTc1MDFhOWU2IiwKICAgICAgICAiYW5hbHlzaXNfc2hhMjU2IjogImJjYjI4YmQwYjVmZjg4M2JiNDhmMzhhYzk0YTYyMWU3NTIwNWRlYjNlZGVjNTlmYTkyYzJlOWRhNzUwMWE5ZTYiCiAgICAgIH0sCiAgICAgICJ0b29scy9hZ2VudC90cmFja2luZy9za2luX3JlcG9ydF90b29sLnB5IjogewogICAgICAgICJiZWZvcmVfc2hhMjU2IjogbnVsbCwKICAgICAgICAicmV2aWV3ZWRfc2hhMjU2IjogImVmMjhkZDNhNjI3YWFjYWY2N2E5YjE5OWMwMTY2MGM0YWJkNzdlNjY1YTFjY2UyYmU0NzE3YjQ2YzJhMGZhNWIiLAogICAgICAgICJhbmFseXNpc19zaGEyNTYiOiAiZWYyOGRkM2E2MjdhYWNhZjY3YTliMTk5YzAxNjYwYzRhYmQ3N2U2NjVhMWNjZTJiZTQ3MTdiNDZjMmEwZmE1YiIKICAgICAgfSwKICAgICAgImFnZW50cy9za2luX3RyYWNraW5nL2V4dHJhY3Rpb24ucHkiOiB7CiAgICAgICAgImJlZm9yZV9zaGEyNTYiOiBudWxsLAogICAgICAgICJyZXZpZXdlZF9zaGEyNTYiOiAiZTI1NTNkZGUzNzg2YjJlNDQzOWI4NmEzNjVmOGEwZjkzOWRlNTBjMTQ3OThlNDkzM2M3ZDBhNmI1MWY1ZjY5OCIsCiAgICAgICAgImFuYWx5c2lzX3NoYTI1NiI6ICJlMjU1M2RkZTM3ODZiMmU0NDM5Yjg2YTM2NWY4YTBmOTM5ZGU1MGMxNDc5OGU0OTMzYzdkMGE2YjUxZjVmNjk4IgogICAgICB9CiAgICB9LAogICAgInJldmlld19zY29wZSI6ICJHdWVzdC93ZWxjb21lIGV4dHJhY3RlZCBpbXBsZW1lbnRhdGlvbnM7IHVuY2hhbmdlZCBtZW1iZXIvY2hhdCBBUEkgQVNUOyBleHBsaWNpdCBza2luX2NvbnN1bHQgcGFyc2VkX2ludGVudCByZXVzZTsgY29sbGVjdGVkIHNraW4tdHJhY2tpbmcgdGltaW5nL3NldmVyaXR5IHRleHQgY2hhbmdlcy4gT2ZmbGluZSByZXZpZXcgYW5kIGZpeHR1cmVzIG9ubHksIG5vdCBydW50aW1lIGFjY2VwdGFuY2UuIiwKICAgICJiZWhhdmlvcl9jaGFuZ2Vfbm90ZSI6ICJDdXJyZW50IG9yY2hlc3RyYXRvciBzdXBwbGllcyBhIGRldGVybWluaXN0aWMgcGFyc2VkX2ludGVudCBmb3IgZXhwbGljaXQgc2tpbl9jb25zdWx0OyBjb21wYXJlIDo4MDAwIGFuZCA6ODIwMCBvbiB0aGlzIHNhbWUgc291cmNlIHNldCwgbm90IGFzIGFuIHVuY2hhbmdlZC1jb2RlIGNvbXBhcmlzb24gdG8gb2xkZXIgR1BUL2d1ZXN0IGJhc2VsaW5lcy4iCiAgfQp9Cg=='}, 'MANIFEST.sha256': {'sha256': '011ae8a2f6ef45b298eff50b38c18c1e53fd1bd50535ac62361cc49233a6b88d', 'base64': 'MzA5ZThmNTU5NmY1ODQwODUzNmZhYmU0MTVjMzJkOWIzZjA3M2ZmYzdmYzU5ZTRjYWNhOWU2MGZkYWMyNDcxOCAgUkVBRE1FLm1kCmQ2YjYwOTViNmI5MTBiYTI2MDEzNzk1ZWIyNTI1MWY3NjE4ZGQ2ZDVjZjc4ZjM5OTA3MGZjOGM4MmMxODY5Y2EgIFNNT0tFX1JFVklFVy5tZAoxYjA2YzQ0NmJhZWVhNmU2ZTkxYTczMmU2OTg4YTk2ZTgyNGU5OGY3YmNlY2EwNWJhMjdhMGU0OTIxYTI4ZjEyICBTT1VSQ0VfTk9URVMubWQKYjMwZmY4MjNmNGVlYTlkZTFmMTVhMTEzMDk1OGY2MmMyZGNiYWNhZDNhMjdhZDA5YmI3NDQ1YjRjZWU4YzVjMiAgVEVTVF9SRVBPUlQubWQKM2YyNTRmNGJlMTM1Y2NjMjhmM2JmYWQzOTA3MWI4NzQ2MjYxN2NiZDIwMTRhMWMyZTE3Y2RhZjUwMmIwNzBkNiAgYWNjb3VudHMuZXhhbXBsZS5qc29uCjY0MzYxOGVjYmRmZjUwZWQzNTlkY2RlMjYxNTUxOTIwMDBlZjYyYzk1NDkyMjkzM2E2ZTNlODM1MWNlMTc3ZTkgIGF1dGhfY2xpZW50cy5weQoxZWUwZWI2ZDBkMzA1MzliMjU5ZTgzZmVmNTM2MzBjN2IyYzdlMWJjMmU4NDA0NWU2YWUyYTczM2MwNzVmZDAzICBiZW5jaC5weQo0OGYxZDI4MGJhMzllNWZkOGQxMDI5YWU1MmU4YzdhOGEyMzgwMmU4NmMyNjVkNWRlOWU2YmNjYmRmOTc2OGZkICBjb21tb24ucHkKMmQwOWQ0NGNkMjZmOTJiNmNlNGM5MWMzYmUxMTg5NDljMzg4MTVkMDhhNGUwZThlMWNlYzcyYzZmMjZiYzVmYSAgY29tcGF0X2dhdGV3YXkucHkKMzY4NjlkMTQzODg5NTYwMzQyYzU4NTAxMjZmY2Q5MWVjNTYwZGE2YzkyZjMxMGQ4ZGZmNTc5YzA2Y2M1OWFlZiAgY29uZmlnLmpzb24KNTQwMGU2YmZiOGRhNDNhNzlmYzc4NWJkMDhkNzA2MzVkODgwOTU1MjNkNmY4NzI0NzU4OWEyMDVjOGIxZmNlNCAgY29uZmlnX3ByZXBhcmUucHkKZGU0OTlkYjI1ZGM4NGZhNDFlMjNiYmU3MWI4ZTA2NDhmYmQ4NmRmMmQxMTI1Y2QyMDljNTM2ODgyY2M0NGRmMSAgY29uZmlndXJlX3Rlc3RfYWNjb3VudHMucHkKOTk4NDY1OGZjNDhjMjNiNDVhMmM1ZGVmYWExYTg5YWYzOGU2NDA5NTZlY2Q2NTVjYzYwNTM0ZDEyOTczMTQ5MiAgcGFja19yZXN1bHRzLnNoCmM3ODg0NGYzZTBjMzI1MTQxMTM0YmVkNmQ0YTAyNTQxNDkzYTZhMzVjN2NjM2RjNzlmODM4NGRiYzNkODk4NDEgIHByb2Nlc3NfY29udHJvbC5weQo5M2Q5MTRlNWUxNzBlYjQ3YmU2ZjY3MzA5ODE1MzkzNGE0MTUxNGUxZDgyZTIyMmZlOTNhYzA2MGQ4YjE0MzQyICBwcm9maWxlX2JhY2tlbmQucHkKMzU0YTM5ODFlODBlNjQ0ZGY4YmI2ZTRmZDA3MTBhNGU3ZDBlOTkyNWUwYjEwNDllZWI2NGQxZGZmYjllYmY3NCAgcmVkaXNfbG9jYWwucHkKNzcwNTRjMmU3NTk2MjgwYWVhNzZjOTA3ODAxZThiNjdmMThhZDUzZjc2OTZiM2ZjNjcyOThiN2NjN2Y2ZDJkYyAgcmVzdG9yZV90ZXN0X3N0YXRlLnNoCmExYjNlODNmOGQ4ZDkxMWY2ZDYxZmExMDQwN2ExYWY3NTU0MWIxYTliMzE1YTdjNDc1YzBjZWFiZjYwMWU3OTYgIHJ1bl9sb2NhbF9wcm92aWRlcl9wYXRoX2JlbmNobWFyay5zaAplYTQyMzAxMmQzN2E1OWNlYjJjNjQ1NjdhYjZjYTA5YjUxOTQ2MTk0MzVhZWRjZGQwMWNlMjYwZGVlMTZiNzFjICBydW5fdmVyaWZpZWRfc2hhZG93X3Rlc3RzLnNoCjcxYTBmYjM2YTJkZDYzNzcxYTMwMzkzYjJmYTIyODdlZDI4NjVkOTZjNWYyNjBhNWRhNDU3ZjI1Mzc5M2EwM2YgIHNlbGZ0ZXN0LnNoCmRjY2VmYzYwM2ZhMjkwMGZlMTUxYTlmMDYzNDIxMmIwYTQzMDVmMGJmNGZiM2EzNTRkNGZmMzJhYmVhMTQ3ZWMgIHNoYWRvd19ib290c3RyYXAucHkKMGJjNTY4ZDZiN2I4NTM5NGI5NGU3ZjU4MmExNjhiZGE5MGY0YjE5NjBhMjQzMGYyYTA0MTczY2VjMDZkNDAzMyAgc2hhZG93X2NvcHkucHkKZGZjZmY4NjQ0OTQ4NTQzZTBjMzAxZDk0NWQwNTkxYmZkOTE3MzUxZDY5YWQ4YmEwNWU5YzFhMzZkMmU5ZDk5ZCAgc2hhZG93X2VudHJ5LnB5CjdhMDBmZTNlNmQ0NTNjN2YwZDg0NjU3Y2VlMmJhZWJhNTgyMzNmYzljMWNmMDAzZGJlYTE1YTVmMmUyZTk2YmIgIHNoYWRvd19ndWFyZC5weQo4NGNiYzAwYjkxOTg0ZTJlMGNkOTQ1NDU2YmJmZTRhYjUwZDFiNDZjMGY0YTBhMzBhMjYxYmVjOGZhZGMwZmIzICBzbW9rZV9jaGVja3MucHkKOWQyZGZjNWJmZjE0MjkzZDBmYTJmMDdjMjE5YjM3N2UxNWMyZTg2YTRlNjMwMzYxMTgxNjM3MzQ1NjI3MWEwOCAgc291cmNlX2NvbnRyYWN0Lmpzb24KZWE3ZDA2YzA1N2Q0MGJiZWRkNzE2ODZhNzVjYzQ3NmFlOWE3N2E5OWM0ZDM5MjU4YWI5YjcwZDJkODBiOGQwNyAgdGVsZW1ldHJ5LnB5CjkxZWU5MmYxODU4YTQ1NDBlYjBhYWM4MjFhMzA3OWI4MDE4MDg1ZTZmNDlkYTM2ZTY1YTdkZTYxYWFhOTQzYjggIHRlc3RzL3Rlc3RfYXV0aF93b3JrZmxvdy5weQoyMDkzYjZiMGMxY2U3MzJlZGZiNWQ0ZjFmNGExYWZlNmNhYjhjZjRiYTY1NmNjNDNkOWQzYTJjNzZjZjU1YzZkICB0ZXN0cy90ZXN0X2J1bmRsZS5weQo='}}
SOURCE_DELTA_ARCHIVE_SHA256 = 'bd3108e3cd6dc04d984c6a0d7b1a9d78c2e591a4bd8e46f588e3f39e6c72436f'
LOCK_NAMES = ('.verified-workflow.lock', '.benchmark.lock')
MAX_FILE_BYTES = 2 * 1024 * 1024

class Refuse(RuntimeError):
    pass

class SourceDrift(Refuse):
    def __init__(self, rows):
        super().__init__('Live source differs from the reviewed delta; no metadata accepted')
        self.rows = rows

def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()

def no_symlink(path: Path) -> None:
    for item in (path, *path.parents):
        if item.is_symlink():
            raise Refuse('Symlink path refused: ' + str(path))

def signature(st):
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)

def read_regular(path: Path, limit: int = MAX_FILE_BYTES):
    no_symlink(path)
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
            raise Refuse('Expected bounded regular file: ' + str(path))
        data = bytearray()
        while True:
            block = os.read(fd, min(65536, limit + 1 - len(data)))
            if not block:
                break
            data.extend(block)
            if len(data) > limit:
                raise Refuse('File grew past size limit: ' + str(path))
        after = os.fstat(fd)
        pathname = path.stat(follow_symlinks=False)
        if signature(before) != signature(after) or signature(after) != signature(pathname):
            raise Refuse('File changed during read: ' + str(path))
        return bytes(data), after
    finally:
        os.close(fd)

def payloads(records):
    out = {}
    for rel, item in records.items():
        raw = base64.b64decode(item['base64'], validate=True)
        if sha(raw) != item['sha256']:
            raise Refuse('Embedded payload is damaged: ' + rel)
        out[rel] = raw
    if set(out) != {'source_contract.json', 'MANIFEST.sha256'}:
        raise Refuse('Unexpected metadata payload targets')
    return out

def manifest_entries(raw: bytes):
    entries = {}
    for line in raw.decode('utf-8').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        if not match:
            raise Refuse('Invalid manifest line')
        digest, rel = match.groups()
        p = PurePosixPath(rel)
        if p.is_absolute() or '..' in p.parts or any(s.startswith('.') for s in p.parts):
            raise Refuse('Unsafe manifest path')
        if rel == 'MANIFEST.sha256' or rel in entries:
            raise Refuse('Duplicate/self manifest entry')
        entries[rel] = digest
    return entries

def check_target(bundle: Path) -> None:
    no_symlink(bundle)
    if not bundle.is_dir() or bundle.name != 'local-llm-path-benchmark' or bundle.parent.name != 'ops':
        raise Refuse('Target must be the existing ops/local-llm-path-benchmark tool directory')
    no_symlink(bundle/'backups')
    if (bundle/'backups').exists() and not (bundle/'backups').is_dir():
        raise Refuse('backups is not a directory')
    state_dir = bundle/'.state'
    no_symlink(state_dir)
    if state_dir.exists() and not state_dir.is_dir():
        raise Refuse('.state is not a directory')
    if state_dir.is_dir():
        for child in state_dir.iterdir():
            no_symlink(child)
            if not child.is_dir():
                raise Refuse('Unreviewed private recovery artifact exists; do not update')
            # Empty directories have no recovery data and are left untouched.
            if not next(child.iterdir(), None):
                continue
            journal = child/'journal.json'
            if not journal.is_file():
                raise Refuse('Unresolved private state without a journal: ' + child.name)
            raw, _ = read_regular(journal)
            try:
                record = json.loads(raw)
            except ValueError:
                raise Refuse('Unreadable recovery journal; resolve recovery first') from None
            if not isinstance(record, dict) or record.get('cleanup_pass') is not True:
                raise Refuse('Incomplete recovery state: ' + child.name)

@contextmanager
def locks(bundle: Path, create: bool):
    handles = []
    try:
        for name in LOCK_NAMES:
            path = bundle/name
            no_symlink(path)
            if not path.exists() and not create:
                continue
            flags = os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
            if create:
                flags |= os.O_CREAT
            fd = os.open(path, flags, 0o600)
            handles.append(fd)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise Refuse('Lock is not a regular file')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refuse('Benchmark/workflow/recovery is running; update refused') from None
        yield
    finally:
        for fd in reversed(handles):
            os.close(fd)

def verify_sources(root: Path, contract: dict):
    no_symlink(root)
    if not root.is_dir():
        raise Refuse('Live source root is not a directory')
    mismatches = []
    rows = []
    for rel, expected in sorted(contract['critical_original_sha256'].items()):
        pure = PurePosixPath(rel)
        if pure.is_absolute() or '..' in pure.parts or pure.suffix != '.py':
            raise Refuse('Unsafe embedded source-contract path')
        if not re.fullmatch('[0-9a-f]{64}', expected):
            raise Refuse('Invalid embedded source digest')
        file = root/rel
        try:
            data, st = read_regular(file)
        except FileNotFoundError:
            mismatches.append({'path': rel, 'expected_sha256': expected, 'actual_sha256': None})
            continue
        actual = sha(data)
        if actual != expected:
            mismatches.append({'path': rel, 'expected_sha256': expected, 'actual_sha256': actual})
            continue
        try:
            ast.parse(data, filename=rel)
        except (SyntaxError, ValueError) as error:
            raise Refuse('Live source syntax failed: ' + rel) from None
        rows.append({'path': rel, 'sha256': actual, 'size_bytes': st.st_size})
    if mismatches:
        raise SourceDrift(mismatches)
    return rows

def plan(bundle: Path):
    check_target(bundle)
    old, new = payloads(BASE_FILES), payloads(NEXT_FILES)
    snapshots, metadata = {}, {}
    for rel in old:
        snapshots[rel], metadata[rel] = read_regular(bundle/rel)
    old_match = all(snapshots[k] == old[k] for k in old)
    new_match = all(snapshots[k] == new[k] for k in new)
    if not old_match and not new_match:
        raise Refuse('Unknown or partially updated source_contract/manifest; restore the metadata backup before retrying')
    current_manifest = manifest_entries(snapshots['MANIFEST.sha256'])
    next_manifest = manifest_entries(new['MANIFEST.sha256'])
    if set(current_manifest) != set(next_manifest):
        raise Refuse('Manifest membership must stay unchanged')
    changed_lines = {k for k in current_manifest if current_manifest[k] != next_manifest[k]}
    if changed_lines - {'source_contract.json'}:
        raise Refuse('This update may only change the source_contract manifest entry')
    for rel, expected in current_manifest.items():
        data, _ = read_regular(bundle/rel)
        if sha(data) != expected:
            raise Refuse('Unreviewed test-tool change: ' + rel)
    cfg_raw, _ = read_regular(bundle/'config.json')
    config = json.loads(cfg_raw)
    root = Path(config['root'])
    if not root.is_absolute():
        raise Refuse('config.root must be absolute')
    no_symlink(root)
    # The existing tool and the read-only source must share the same tomod root.
    if root.resolve() != bundle.parent.parent.resolve():
        raise Refuse('Bundle location differs from config.root; refusing to guess live source')
    source = root/'cosmetic-agent'
    if source.resolve() == bundle.resolve() or source.resolve().is_relative_to(bundle.resolve()):
        raise Refuse('Unsafe live source location')
    review = json.loads(new['source_contract.json'])
    rows = verify_sources(source, review)
    return {'already_current': new_match, 'before': snapshots, 'stats': metadata,
            'next': new, 'source': source, 'source_rows': rows, 'contract': review}

def atomic_write(path: Path, data: bytes, metadata=None) -> None:
    no_symlink(path)
    fd, temporary = tempfile.mkstemp(prefix='.reviewed-contract-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            mode = stat.S_IMODE(metadata.st_mode) if metadata else 0o600
            os.fchmod(stream.fileno(), mode)
            if metadata and (metadata.st_uid != os.geteuid() or metadata.st_gid != os.getegid()):
                os.fchown(stream.fileno(), metadata.st_uid, metadata.st_gid)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        dfd = os.open(path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try: os.fsync(dfd)
        finally: os.close(dfd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def update(bundle: Path, apply: bool = False):
    check_target(bundle)
    with locks(bundle, create=apply):
        p = plan(bundle)
        summary = {'mode': 'already_current' if p['already_current'] else 'apply' if apply else 'check_only',
                   'pass': True, 'reviewed_delta': p['contract']['snapshot'],
                   'verified_live_source_files': len(p['source_rows']),
                   'managed_files_changed': 0, 'planned_metadata_changes': 0 if p['already_current'] else 2,
                   'service_actions': 0, 'backend_files_written': 0,
                   'credential_files_read': 0, 'test_code_or_configuration_changed': False}
        if p['already_current'] or not apply:
            return summary
        # Check all 29 files again immediately before the metadata transaction.
        verify_sources(p['source'], p['contract'])
        tag = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S-%f') + '-' + uuid.uuid4().hex[:6]
        backup = bundle/'backups'/('reviewed-source-contract-' + tag)
        no_symlink(backup)
        backup.mkdir(parents=True, mode=0o700, exist_ok=False)
        for rel, data in p['before'].items():
            atomic_write(backup/rel, data)
        audit = {'source_delta_archive_sha256': SOURCE_DELTA_ARCHIVE_SHA256,
                 'reviewed_delta': p['contract']['snapshot'],
                 'metadata_files': {k:{'before_sha256':sha(v), 'after_sha256':sha(p['next'][k])} for k,v in p['before'].items()},
                 'verified_live_sources':p['source_rows'], 'production_code_write':False}
        atomic_write(backup/'update-plan.json', (json.dumps(audit, indent=2)+'\n').encode())
        attempted = []
        try:
            # Manifest last: an interrupted partial update does not falsely verify.
            for rel in ('source_contract.json', 'MANIFEST.sha256'):
                current, _ = read_regular(bundle/rel)
                if current != p['before'][rel]:
                    raise Refuse('Metadata changed during the update: ' + rel)
                attempted.append(rel)
                atomic_write(bundle/rel, p['next'][rel], p['stats'][rel])
            # Whole-tool and whole-source verification; no source import.
            after = plan(bundle)
            if not after['already_current']:
                raise Refuse('Post-update verification did not match the reviewed payload')
        except BaseException:
            rollback_errors = []
            for rel in reversed(attempted):
                try:
                    now, _ = read_regular(bundle/rel)
                    if now == p['before'][rel]:
                        continue
                    if now != p['next'][rel]:
                        raise Refuse('Concurrent external metadata edit; refusing overwrite')
                    atomic_write(bundle/rel, p['before'][rel], p['stats'][rel])
                except BaseException:
                    rollback_errors.append(rel)
            if rollback_errors:
                atomic_write(backup/'ROLLBACK_FAILED.json', (json.dumps({'paths':rollback_errors})+'\n').encode())
                raise Refuse('Metadata rollback needs review; preserved backup: ' + str(backup)) from None
            raise
        summary.update(managed_files_changed=2, backup=str(backup))
        return summary

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--bundle-dir', default=str(Path(__file__).absolute().parent))
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--check-only', action='store_true', help='Read-only check (also the default)')
    mode.add_argument('--apply', action='store_true', help='Back up and update only two metadata files')
    args = ap.parse_args()
    os.umask(0o077)
    result = update(Path(args.bundle_dir).absolute(), apply=args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SourceDrift as error:
        print(json.dumps({'pass':False,'reason':str(error),'mismatches':error.rows,
                          'backend_files_written':0,'service_actions':0},ensure_ascii=False,indent=2),file=sys.stderr)
        raise SystemExit(2)
    except (Refuse, OSError, ValueError, KeyError) as error:
        print('[REFUSED]', str(error), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print('[INTERRUPTED] No service commands were issued. Review metadata/backup before retrying.',file=sys.stderr)
        raise SystemExit(130)
