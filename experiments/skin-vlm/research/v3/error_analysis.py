"""Validation-only error analysis and candidate selection for cycle 3."""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

try:
    from .facial_research import write_json
except ImportError:
    from facial_research import write_json


def read_jsonl(path: Path) -> List[Dict[str,Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def binary_metrics(rows: Sequence[Mapping[str,Any]]) -> Dict[str,float]:
    counts=Counter()
    for row in rows:
        label=row.get("label");pred=row.get("prediction")
        if type(label) is not bool or type(pred) is not bool:
            counts["invalid"]+=1
        elif label and pred: counts["tp"]+=1
        elif label: counts["fn"]+=1
        elif pred: counts["fp"]+=1
        else: counts["tn"]+=1
    tp,fp,tn,fn=(counts[x] for x in ("tp","fp","tn","fn"))
    div=lambda a,b:a/b if b else 0.
    pos_f1=div(2*tp,2*tp+fp+fn);neg_f1=div(2*tn,2*tn+fp+fn)
    return {"n":len(rows),"tp":tp,"fp":fp,"tn":tn,"fn":fn,"invalid":counts["invalid"],
            "precision":div(tp,tp+fp),"recall":div(tp,tp+fn),"specificity":div(tn,tn+fp),
            "macro_f1":(pos_f1+neg_f1)/2,"accuracy":div(tp+tn,len(rows))}


def paired_audit(baseline: Sequence[Mapping[str,Any]], candidate: Sequence[Mapping[str,Any]]) -> Dict[str,Any]:
    base={x["id"]:x for x in baseline}; new={x["id"]:x for x in candidate}
    if set(base)!=set(new): raise ValueError("paired files must contain identical IDs")
    transitions=Counter();regressed=[];corrected=[]
    for item_id in sorted(base):
        b=base[item_id];c=new[item_id]
        if b.get("label")!=c.get("label") or b.get("group_id")!=c.get("group_id"):
            raise ValueError("paired labels/groups changed")
        key="label_%s__base_%s__candidate_%s"%(str(b["label"]).lower(),str(b["prediction"]).lower(),str(c["prediction"]).lower())
        transitions[key]+=1
        if b["prediction"]==b["label"] and c["prediction"]!=b["label"]: regressed.append(item_id)
        if b["prediction"]!=b["label"] and c["prediction"]==b["label"]: corrected.append(item_id)
    bm=binary_metrics(list(base.values()));cm=binary_metrics(list(new.values()))
    precision_delta=cm["precision"]-bm["precision"];recall_delta=cm["recall"]-bm["recall"]
    base_positive=bm["tp"]+bm["fp"];candidate_positive=cm["tp"]+cm["fp"]
    diagnosis=("candidate_became_too_conservative" if recall_delta<0 and candidate_positive<base_positive else
               "candidate_became_too_permissive" if cm["specificity"]<bm["specificity"] and candidate_positive>base_positive else
               "mixed_or_uniform_shift")
    return {"baseline":bm,"candidate":cm,"delta":{"precision":precision_delta,"recall":recall_delta,
            "macro_f1":cm["macro_f1"]-bm["macro_f1"]},"transitions":dict(transitions),
            "regressed_ids":regressed,"corrected_ids":corrected,"failure_pattern":diagnosis,
            "use_restriction":"IDs are audit evidence only. Locked-test errors must not be copied into training or used to choose thresholds."}


def stratified_metrics(rows: Sequence[Mapping[str,Any]], fields: Sequence[str], minimum_n: int=20) -> Dict[str,Any]:
    result={}
    for field in fields:
        buckets=defaultdict(list)
        for row in rows: buckets[str(row.get(field,"missing"))].append(row)
        result[field]={key:{"claimable":len(items)>=minimum_n,"metrics":binary_metrics(items)}
                       for key,items in sorted(buckets.items())}
    return result


def choose_validation_candidate(candidates: Sequence[Mapping[str,Any]]) -> Dict[str,Any]:
    """Apply predeclared floors; never accepts test results as input."""
    eligible=[];rejected=[]
    for candidate in candidates:
        if candidate.get("split")!="validation": raise ValueError("candidate selection may only read validation results")
        m=candidate["metrics"]
        reasons=[]
        if m.get("valid_json_rate",1)<.98: reasons.append("valid_json_below_0.98")
        if m.get("precision",m.get("positive_precision",0))<.85: reasons.append("precision_below_0.85")
        if m.get("recall",m.get("positive_recall",0))<.85: reasons.append("recall_below_0.85")
        if m.get("macro_f1",0)<.85: reasons.append("macro_f1_below_0.85")
        (rejected if reasons else eligible).append({"name":candidate["name"],"reasons":reasons})
    if not eligible:return {"selected":None,"eligible":[],"rejected":rejected,"stop":True}
    names={x["name"] for x in eligible}
    pool=[x for x in candidates if x["name"] in names]
    winner=max(pool,key=lambda x:(x["metrics"]["macro_f1"],x["metrics"].get("recall",x["metrics"].get("positive_recall",0)),-x.get("trainable_parameters",0)))
    return {"selected":winner["name"],"eligible":sorted(names),"rejected":rejected,"stop":False,
            "rule":"floors first; then macro-F1, recall, fewer trainable parameters"}


def main() -> None:
    p=argparse.ArgumentParser();p.add_argument("--baseline",type=Path,required=True)
    p.add_argument("--candidate",type=Path,required=True);p.add_argument("--output",type=Path,required=True)
    a=p.parse_args();result=paired_audit(read_jsonl(a.baseline),read_jsonl(a.candidate));write_json(a.output,result)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":main()

