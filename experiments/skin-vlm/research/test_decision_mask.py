"""Verify decision-only supervision against the actual pinned tokenizer."""
from pathlib import Path
from transformers import AutoProcessor
import benchmark_v2 as b

def main():
    processor=AutoProcessor.from_pretrained(Path(__file__).resolve().parent.parent/'model',trust_remote_code=False,local_files_only=True)
    processor.image_processor.size={'shortest_edge':128*32*32,'longest_edge':384*32*32}
    rows=b.read_jsonl(b.ROOT/'data/frozen-v2/train.jsonl')
    for wanted in [True,False]:
        row=next(r for r in rows if r['lesion_present']==wanted)
        batch=b.encode(processor,row,training=True);supervised=batch['labels'][batch['labels']!=-100]
        assert supervised.numel()==1
        assert processor.tokenizer.decode(supervised)=='true' if wanted else processor.tokenizer.decode(supervised)=='false'
    print('DECISION_MASK_PASSED: exactly one correct boolean token for both classes; prompt/image/scaffold masked')

if __name__=='__main__':main()
