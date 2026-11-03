"""Publish aggregate progress during this bounded experiment chain, then exit."""
import json,time,traceback
from pathlib import Path
from publish_comparison import main as publish

ROOT=Path(__file__).resolve().parent
deadline=time.monotonic()+3600
while time.monotonic()<deadline:
    try:
        publish()
        status_path=ROOT/'cycles/v2/status.json'
        if status_path.exists() and json.loads(status_path.read_text())['phase'] in {'complete','failed'}:break
    except Exception:traceback.print_exc()
    time.sleep(20)
