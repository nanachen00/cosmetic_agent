"""Run cycle-3 contract tests or evaluate real detector/colour/multi-view records."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Dict

import numpy as np
from PIL import Image

try:
    from .facial_research import (build_qwen_evidence, controlled_variants, delta_e_2000,
        detection_metrics, multiview_consistency, personal_baseline, robust_roi_lab,
        srgb_to_lab, write_json)
except ImportError:  # direct script execution on the research server
    from facial_research import (build_qwen_evidence, controlled_variants, delta_e_2000,
        detection_metrics, multiview_consistency, personal_baseline, robust_roi_lab,
        srgb_to_lab, write_json)

ROOT=Path(__file__).resolve().parent


def self_check() -> Dict[str,Any]:
    base=Image.new("RGB",(64,64),(170,105,90)); variants=controlled_variants(base)
    mask=np.ones((64,64),dtype=bool)
    clean_colour=robust_roi_lab(base,mask)
    colour_probe={name:delta_e_2000(clean_colour["lab_median"],robust_roi_lab(im,mask)["lab_median"])
                  for name,im in variants.items()}
    gt={"image-a":[[5,5,20,20],[30,30,45,45]],"image-b":[]}
    preds={"image-a":[{"box":[5,5,20,20],"score":.95},{"box":[0,0,4,4],"score":.2}],"image-b":[]}
    detector=detection_metrics(gt,preds)
    findings={"front":[{"tag":"redness","region":"left_cheek","evidence":"visible"}],
              "left":[{"tag":"redness","region":"left_cheek","evidence":"visible"}],"right":[]}
    multiview=multiview_consistency(findings)
    history=[{"subject_id":"demo","device_id":"phone-a","capture_profile":"fixed-v1","region":"left_cheek",
              "captured_date":"2026-10-%02d"%day,"lesion_count":count,"redness_a_star":red}
             for day,count,red in [(1,2,13.0),(8,3,13.5),(15,2,12.5)]]
    current={"subject_id":"demo","device_id":"phone-a","capture_profile":"fixed-v1","region":"left_cheek",
             "captured_date":"2026-11-01","lesion_count":4,"redness_a_star":14.0}
    baseline=personal_baseline(history,current)
    colour={"clean_lab":clean_colour["lab_median"],"synthetic_delta_e00":colour_probe,
            "status":"synthetic_robustness_probe_only"}
    packet=build_qwen_evidence(detector,colour,multiview,baseline)
    return {"status":"contract_pipeline_passed","five_tracks":{
        "1_detection":{"status":"passed","metrics":detector},
        "2_colour":{"status":"passed","metrics":colour},
        "3_multiview":{"status":"passed","metrics":multiview},
        "4_personal_baseline":{"status":"passed","metrics":baseline},
        "5_qwen_integration":{"status":"passed","packet_sha256":hashlib.sha256(json.dumps(packet,sort_keys=True).encode()).hexdigest()}},
        "claim_limit":"Synthetic contract run only. It does not establish real-image accuracy, colour calibration, height estimation, or clinical validity.",
        "packet":packet}


def evaluate(path: Path) -> Dict[str,Any]:
    data=json.loads(path.read_text(encoding="utf-8"))
    required={"ground_truth_boxes","predicted_boxes","views","history","current","colour"}
    if set(data)<required: raise ValueError("evaluation input missing: "+", ".join(sorted(required-set(data))))
    detector=detection_metrics(data["ground_truth_boxes"],data["predicted_boxes"])
    multiview=multiview_consistency(data["views"])
    baseline=personal_baseline(data["history"],data["current"])
    packet=build_qwen_evidence(detector,data["colour"],multiview,baseline)
    return {"status":"real_record_evaluation_complete","detector":detector,"multiview":multiview,
            "personal_baseline":baseline,"qwen_packet":packet}


def main() -> None:
    parser=argparse.ArgumentParser()
    parser.add_argument("--input",type=Path,help="real evaluation record JSON")
    parser.add_argument("--output",type=Path,default=ROOT/"records"/"self-check.json")
    args=parser.parse_args()
    result=evaluate(args.input) if args.input else self_check()
    write_json(args.output,result);print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__": main()

