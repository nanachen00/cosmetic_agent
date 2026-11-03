"""Download only official safetensors/data files, pinning the exact model commit."""
import os
from pathlib import Path
os.environ['HF_HUB_DISABLE_XET']='1'
from huggingface_hub import HfApi,snapshot_download

if __name__=='__main__':
    root=Path(__file__).resolve().parent
    revision=HfApi().model_info('Qwen/Qwen3-VL-4B-Instruct').sha
    (root/'model_revision.txt').write_text(revision)
    print('Pinned revision',revision,flush=True)
    snapshot_download('Qwen/Qwen3-VL-4B-Instruct',revision=revision,local_dir=root/'model',
                      allow_patterns=['*.json','*.safetensors','*.txt','*.jinja','README.md','LICENSE*'],max_workers=2)
    (root/'download.done').write_text(revision)
    print('DOWNLOAD_COMPLETE',flush=True)
