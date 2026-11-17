"""Small validation-only runtime comparison, never a test/accuracy claim."""
import argparse
from importlib.metadata import version
import json
from pathlib import Path
import resource
import sys

from train_cycle3 import require_persistent_mount


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--model',type=Path,required=True)
    parser.add_argument('--validation',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--attention',choices=('sdpa','eager'),default='sdpa')
    args=parser.parse_args();require_persistent_mount([args.model,args.validation,args.output])
    resource.setrlimit(resource.RLIMIT_CORE,(0,0))
    if args.validation.name!='validation.jsonl':raise ValueError('Use the validation manifest only')
    import torch
    from transformers import AutoProcessor,Qwen3VLForConditionalGeneration
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]));import benchmark_v2 as legacy
    sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
    from rocm_patch_projection import configure_rocm_patch_projection
    processor=AutoProcessor.from_pretrained(args.model,local_files_only=True,trust_remote_code=False)
    processor.image_processor.size={'shortest_edge':128*32*32,'longest_edge':384*32*32}
    model=Qwen3VLForConditionalGeneration.from_pretrained(args.model,dtype=torch.bfloat16,
        local_files_only=True,trust_remote_code=False,use_safetensors=True,attn_implementation=args.attention)
    configure_rocm_patch_projection(model);model=model.to('cuda:0').eval()
    rows=legacy.read_jsonl(args.validation)[:8]
    args.output.mkdir(parents=True,exist_ok=True)
    metrics,predictions=legacy.evaluate(model,processor,rows,args.output/'validation-probe.jsonl')
    report={'engineering_comparison_only':True,'selection_or_test_claim':False,
            'transformers':version('transformers'),'torch':version('torch'),'attention':args.attention,
            'metrics':metrics,'predictions':predictions}
    (args.output/'result.json').write_text(json.dumps(report,indent=2,ensure_ascii=False))
    print(json.dumps(report,indent=2,ensure_ascii=False))


if __name__=='__main__':main()
