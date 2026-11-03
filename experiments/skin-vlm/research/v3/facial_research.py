"""Reusable contracts and metrics for the five cycle-3 research tracks.

Synthetic transforms in this module are robustness probes. They are not a
substitute for calibrated colour targets, true multi-view captures, or expert
lesion annotations.
"""
from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter


VIEWS = ("front", "left", "right")
VISIBLE_TAGS = {"acne_like_bumps", "redness", "dark_marks", "flaking"}
FACE_REGIONS = {
    "forehead", "left_cheek", "right_cheek", "nose", "chin", "eye_area", "unknown"
}


def _finite(value: float, name: str) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("non-finite " + name)
    return value


def srgb_to_lab(rgb: Sequence[float]) -> Tuple[float, float, float]:
    """Convert an sRGB triplet (0..255) to CIE L*a*b* using D65."""
    if len(rgb) != 3:
        raise ValueError("RGB must have exactly three values")
    x = np.asarray(rgb, dtype=np.float64) / 255.0
    if np.any(x < 0) or np.any(x > 1):
        raise ValueError("RGB values must be in 0..255")
    x = np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)
    xyz = np.array([
        x[0] * .4124564 + x[1] * .3575761 + x[2] * .1804375,
        x[0] * .2126729 + x[1] * .7151522 + x[2] * .0721750,
        x[0] * .0193339 + x[1] * .1191920 + x[2] * .9503041,
    ]) / np.array([.95047, 1.0, 1.08883])
    delta = 6 / 29
    f = np.where(xyz > delta ** 3, np.cbrt(xyz), xyz / (3 * delta ** 2) + 4 / 29)
    return float(116 * f[1] - 16), float(500 * (f[0] - f[1])), float(200 * (f[1] - f[2]))


def delta_e_2000(lab1: Sequence[float], lab2: Sequence[float]) -> float:
    """CIEDE2000 colour difference (kL=kC=kH=1)."""
    if len(lab1) != 3 or len(lab2) != 3:
        raise ValueError("Lab values must have exactly three components")
    l1, a1, b1 = map(float, lab1); l2, a2, b2 = map(float, lab2)
    c1 = math.hypot(a1, b1); c2 = math.hypot(a2, b2); cbar = (c1 + c2) / 2
    g = .5 * (1 - math.sqrt(cbar ** 7 / (cbar ** 7 + 25 ** 7)))
    ap1 = (1 + g) * a1; ap2 = (1 + g) * a2
    cp1 = math.hypot(ap1, b1); cp2 = math.hypot(ap2, b2)
    hp1 = math.degrees(math.atan2(b1, ap1)) % 360 if cp1 else 0
    hp2 = math.degrees(math.atan2(b2, ap2)) % 360 if cp2 else 0
    dl = l2 - l1; dc = cp2 - cp1
    dh_angle = hp2 - hp1
    if cp1 * cp2 == 0: dh_angle = 0
    elif dh_angle > 180: dh_angle -= 360
    elif dh_angle < -180: dh_angle += 360
    dh = 2 * math.sqrt(cp1 * cp2) * math.sin(math.radians(dh_angle / 2))
    lbar = (l1 + l2) / 2; cpbar = (cp1 + cp2) / 2
    if cp1 * cp2 == 0: hpbar = hp1 + hp2
    elif abs(hp1 - hp2) <= 180: hpbar = (hp1 + hp2) / 2
    elif hp1 + hp2 < 360: hpbar = (hp1 + hp2 + 360) / 2
    else: hpbar = (hp1 + hp2 - 360) / 2
    t = (1 - .17 * math.cos(math.radians(hpbar - 30))
         + .24 * math.cos(math.radians(2 * hpbar))
         + .32 * math.cos(math.radians(3 * hpbar + 6))
         - .20 * math.cos(math.radians(4 * hpbar - 63)))
    sl = 1 + .015 * (lbar - 50) ** 2 / math.sqrt(20 + (lbar - 50) ** 2)
    sc = 1 + .045 * cpbar; sh = 1 + .015 * cpbar * t
    rt = -2 * math.sqrt(cpbar ** 7 / (cpbar ** 7 + 25 ** 7)) * math.sin(
        math.radians(60 * math.exp(-((hpbar - 275) / 25) ** 2)))
    return math.sqrt((dl / sl) ** 2 + (dc / sc) ** 2 + (dh / sh) ** 2
                     + rt * (dc / sc) * (dh / sh))


def robust_roi_lab(image: Image.Image, mask: np.ndarray) -> Dict[str, Any]:
    """Return median Lab after excluding dark/specular pixels from an explicit ROI."""
    rgb = np.asarray(image.convert("RGB"), dtype=np.float64)
    if mask.shape != rgb.shape[:2]:
        raise ValueError("ROI mask shape does not match image")
    selected = rgb[np.asarray(mask, dtype=bool)]
    if len(selected) < 100:
        raise ValueError("ROI needs at least 100 pixels")
    luminance = selected.mean(axis=1)
    low=float(np.percentile(luminance,10)); high=float(np.percentile(luminance,95))
    keep = np.ones(len(luminance),dtype=bool) if abs(high-low)<1e-9 else ((luminance >= low) & (luminance <= high))
    selected = selected[keep]
    if len(selected) < 50:
        raise ValueError("too few usable ROI pixels after shadow/specular exclusion")
    labs = np.asarray([srgb_to_lab(x) for x in selected])
    return {"lab_median": [round(float(x), 4) for x in np.median(labs, axis=0)],
            "usable_pixels": int(len(selected)), "method": "explicit ROI; luminance p10-p95; median Lab"}


def controlled_variants(image: Image.Image) -> Dict[str, Image.Image]:
    """Predeclared one-factor-at-a-time and limited interaction probes."""
    im = image.convert("RGB")
    array = np.asarray(im, dtype=np.float32)
    def balance(r: float, g: float, b: float) -> Image.Image:
        return Image.fromarray(np.clip(array * np.array([r, g, b]), 0, 255).astype(np.uint8))
    shadow = np.ones((im.height, im.width, 1), dtype=np.float32)
    shadow[:, :max(1, im.width // 2)] = .55
    return {
        "clean": im.copy(),
        "exposure_ev_minus_1": ImageEnhance.Brightness(im).enhance(.5),
        "exposure_ev_plus_1": ImageEnhance.Brightness(im).enhance(2.0),
        "white_balance_warm": balance(1.12, 1.0, .86),
        "white_balance_cool": balance(.86, 1.0, 1.12),
        "blur_radius_2": im.filter(ImageFilter.GaussianBlur(2)),
        "directional_shadow": Image.fromarray(np.clip(array * shadow, 0, 255).astype(np.uint8)),
        "warm_plus_shadow": Image.fromarray(
            np.clip(np.asarray(balance(1.12, 1.0, .86), dtype=np.float32) * shadow, 0, 255).astype(np.uint8)),
    }


def iou(box_a: Sequence[float], box_b: Sequence[float]) -> float:
    ax1, ay1, ax2, ay2 = map(float, box_a); bx1, by1, bx2, by2 = map(float, box_b)
    inter = max(0., min(ax2, bx2) - max(ax1, bx1)) * max(0., min(ay2, by2) - max(ay1, by1))
    union = max(0., ax2 - ax1) * max(0., ay2 - ay1) + max(0., bx2 - bx1) * max(0., by2 - by1) - inter
    return inter / union if union else 0.


def detection_metrics(ground_truth: Mapping[str, Sequence[Sequence[float]]],
                      predictions: Mapping[str, Sequence[Mapping[str, Any]]],
                      threshold: float = .5) -> Dict[str, float]:
    """One-class AP@IoU threshold, precision/recall and per-image count MAE."""
    ids = sorted(set(ground_truth) | set(predictions)); total_gt = sum(len(ground_truth.get(x, [])) for x in ids)
    ranked = []
    for image_id in ids:
        for pred in predictions.get(image_id, []):
            ranked.append((float(pred["score"]), image_id, pred["box"]))
    ranked.sort(reverse=True, key=lambda x: x[0])
    used = {x: set() for x in ids}; tp=[]; fp=[]
    for _, image_id, box in ranked:
        candidates = [(iou(box, gt), idx) for idx, gt in enumerate(ground_truth.get(image_id, [])) if idx not in used[image_id]]
        best = max(candidates, default=(0., -1))
        if best[0] >= threshold:
            used[image_id].add(best[1]); tp.append(1); fp.append(0)
        else: tp.append(0); fp.append(1)
    cumulative_tp=np.cumsum(tp); cumulative_fp=np.cumsum(fp)
    recall=cumulative_tp/max(1,total_gt); precision=cumulative_tp/np.maximum(1,cumulative_tp+cumulative_fp)
    mrec=np.concatenate(([0.],recall,[1.])); mpre=np.concatenate(([0.],precision,[0.]))
    for i in range(len(mpre)-2,-1,-1): mpre[i]=max(mpre[i],mpre[i+1])
    changes=np.where(mrec[1:]!=mrec[:-1])[0]; ap=float(np.sum((mrec[changes+1]-mrec[changes])*mpre[changes+1]))
    matched=int(cumulative_tp[-1]) if len(cumulative_tp) else 0; predicted=len(ranked)
    count_mae=statistics.mean(abs(len(predictions.get(x,[]))-len(ground_truth.get(x,[]))) for x in ids) if ids else 0.
    return {"iou_threshold":threshold,"ap":ap,"precision":matched/predicted if predicted else 0.,
            "recall":matched/total_gt if total_gt else 0.,"count_mae":float(count_mae),
            "ground_truth_boxes":total_gt,"predicted_boxes":predicted,"matched_boxes":matched}


def validate_finding(finding: Mapping[str, Any]) -> Tuple[str, str]:
    if set(finding) - {"tag", "region", "evidence", "confidence", "source"}:
        raise ValueError("unsupported finding field")
    tag=str(finding.get("tag")); region=str(finding.get("region"))
    if tag not in VISIBLE_TAGS or region not in FACE_REGIONS:
        raise ValueError("unsupported visible finding")
    return tag, region


def multiview_consistency(views: Mapping[str, Sequence[Mapping[str, Any]]]) -> Dict[str, Any]:
    if set(views) != set(VIEWS):
        raise ValueError("front, left and right views are all required")
    by_key: Dict[Tuple[str,str], List[str]] = {}
    for view in VIEWS:
        seen=set()
        for finding in views[view]:
            key=validate_finding(finding)
            if key in seen: continue
            seen.add(key);by_key.setdefault(key,[]).append(view)
    findings=[]
    for key,present in sorted(by_key.items()):
        score=len(present)/3
        findings.append({"tag":key[0],"region":key[1],"present_views":present,
                         "view_count":len(present),"consistency":round(score,4),
                         "status":"repeated" if len(present)>=2 else "single_view_only"})
    return {"complete_views":True,"findings":findings,
            "repeated_fraction":sum(x["view_count"]>=2 for x in findings)/len(findings) if findings else 1.0,
            "limitation":"Cross-view repetition is not proof of surface height; calibrated stereo or photometric data is required."}


def personal_baseline(history: Sequence[Mapping[str, Any]], current: Mapping[str, Any]) -> Dict[str, Any]:
    """Compare one region with >=3 compatible sessions using median and MAD."""
    keys=("subject_id","device_id","capture_profile","region")
    if any(current.get(k) in (None,"") for k in keys): raise ValueError("current record lacks baseline grouping fields")
    eligible=[x for x in history if all(x.get(k)==current.get(k) for k in keys)]
    if len({x.get("captured_date") for x in eligible}) < 3:
        return {"status":"insufficient_history","eligible_sessions":len(eligible),"required_sessions":3}
    fields=("lesion_count","redness_a_star")
    deltas={}
    for field in fields:
        values=[_finite(x[field],field) for x in eligible]
        median=statistics.median(values); mad=statistics.median(abs(x-median) for x in values)
        value=_finite(current[field],field); robust_z=(value-median)/(1.4826*mad) if mad>1e-9 else None
        deltas[field]={"current":value,"baseline_median":median,"delta":value-median,
                       "baseline_mad":mad,"robust_z":robust_z}
    return {"status":"relative_change_only","eligible_sessions":len(eligible),"deltas":deltas,
            "limitation":"Relative diary comparison only; no disease diagnosis or treatment claim."}


def build_qwen_evidence(detector: Mapping[str, Any], colour: Mapping[str, Any],
                        multiview: Mapping[str, Any], baseline: Mapping[str, Any]) -> Dict[str, Any]:
    """Strict evidence packet for Qwen; numeric modules remain the source of truth."""
    packet={"schema":"blink.skin_evidence.v1","task":"daily_skin_diary_explanation",
            "detector":dict(detector),"colour":dict(colour),"multiview":dict(multiview),
            "personal_baseline":dict(baseline),
            "output_rules":["describe only visible appearance","separate image evidence from symptoms",
                            "do not diagnose disease","do not infer treatment efficacy",
                            "state missing or inconsistent evidence"]}
    json.dumps(packet,allow_nan=False)
    return packet


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(path.suffix+".tmp")
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding="utf-8")
    temp.replace(path)

