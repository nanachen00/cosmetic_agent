#!/usr/bin/env python3
import platform, sys
print('python:', sys.executable)
print('python_version:', platform.python_version())
try:
    import torch
except Exception as exc:
    raise SystemExit(f'[ERROR] torch import failed: {type(exc).__name__}: {exc}')
print('torch:', torch.__version__)
print('torch.cuda.is_available:', torch.cuda.is_available())
print('torch.version.cuda:', torch.version.cuda)
print('torch.version.hip:', torch.version.hip)
if not torch.cuda.is_available():
    raise SystemExit('[ERROR] accelerator unavailable')
if torch.version.hip is None:
    raise SystemExit('[ERROR] ROCm/HIP PyTorch required')
print('device:', torch.cuda.get_device_name(0))
try:
    import sglang
except Exception as exc:
    raise SystemExit(f'[ERROR] sglang import failed: {type(exc).__name__}: {exc}')
print('sglang:', getattr(sglang, '__version__', 'version-unknown'))
try:
    import transformers
    print('transformers:', transformers.__version__)
except Exception as exc:
    print('[WARN] transformers import failed:', exc)
print('[OK] SGLang runtime is ROCm-capable.')
