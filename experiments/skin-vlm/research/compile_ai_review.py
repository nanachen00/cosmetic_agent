"""Bind the assistant's blinded visual notes to immutable SCIN image bytes.

Produces review evidence, never human/expert gold and never a training split.
"""
from __future__ import annotations
import argparse,collections,hashlib,json
from pathlib import Path


def main():
    p=argparse.ArgumentParser();p.add_argument('--queue',type=Path,required=True)
    p.add_argument('--sheets',type=Path,required=True);p.add_argument('--output',type=Path,required=True);args=p.parse_args()
    source=[json.loads(line) for line in args.queue.read_text().splitlines() if line.strip()]
    index=json.loads((args.sheets/'index.json').read_text());decisions=json.loads((args.sheets/'decisions.json').read_text())
    if decisions['human_expert_verified'] or decisions['diagnoses_used']:raise ValueError('This protocol is blinded AI review only')
    if len(source)!=len(index) or sorted(row[0] for row in decisions['rows'])!=list(range(len(source))):raise ValueError('Incomplete or duplicate visual review')
    codes=decisions['codes'];review=[];sha_groups=collections.defaultdict(list)
    for n,positive,uncertain,flags,scope,evidence in decisions['rows']:
        src=source[n];entry=index[n];path=args.queue.parent/src['image']
        if src['id']!=entry['id'] or src['sha256']!=entry['sha256'] or hashlib.sha256(path.read_bytes()).hexdigest()!=src['sha256']:
            raise ValueError('Image/review identity changed')
        if set(positive)&set(uncertain) or not set(positive+uncertain)<=set(codes):raise ValueError('Conflicting or unknown review code')
        quality=[value for value in flags.split(',') if value]
        row=dict(id=src['id'],image=str(path.resolve()),image_sha256=src['sha256'],source_group=src['group_id'],
            label_origin='ai_visual_review',reviewer_type='conversation_assistant',human_expert_verified=False,
            source_diagnosis_hints_shown=False,blinded_review_index=n,image_scope=scope,
            tag_decisions={tag:True if code in positive else None if code in uncertain else False for code,tag in codes.items()},
            visible_evidence=evidence,quality_flags=quality,training_eligible=False,research_only=True,
            status='AI_review_complete_requires_expert_confirmation',
            silver_candidate=not uncertain and not set(quality)-{'other_body_region'},
            confidence_type='qualitative review; no calibrated probability')
        sha_groups[src['sha256']].append(src['id']);review.append(row)
    for row in review:
        row['exact_duplicate_group']=sorted(sha_groups[row['image_sha256']])
        if len(row['exact_duplicate_group'])>1:row['silver_candidate']=False
    if args.output.exists():raise FileExistsError('Do not replace a frozen review')
    args.output.mkdir(parents=True)
    (args.output/'ai-review.jsonl').write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in review))
    files=[args.queue,args.sheets/'index.json',args.sheets/'decisions.json',*sorted(args.sheets.glob('sheet-*.jpg'))]
    report=dict(images_reviewed=len(review),unique_image_byte_hashes=len(sha_groups),
        duplicate_groups=[values for values in sha_groups.values() if len(values)>1],
        uncertain_review_count=sum(any(value is None for value in row['tag_decisions'].values()) for row in review),
        quality_flag_counts=dict(collections.Counter(flag for row in review for flag in row['quality_flags'])),
        potential_silver_candidates=sum(row['silver_candidate'] for row in review),training_ready_gold=0,
        source_hashes={str(file.resolve()):hashlib.sha256(file.read_bytes()).hexdigest() for file in files},
        limitations=['AI visual review is not human/dermatologist adjudication.','Unknown labels are explicit null, never automatic negatives.',
                     'These records are not test/validation gold and were not automatically placed into training.'])
    (args.output/'review-summary.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
