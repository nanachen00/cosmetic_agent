"""Equivalent linear evaluation of Qwen's non-overlapping 3-D patch projection.

ROCm 6.4/MIOpen can segfault on the BF16 Conv3d kernel used by Qwen3-VL.
The projection receives exactly one kernel-sized patch per batch item, so its
single output equals a matrix multiplication with the flattened kernel. The
weight and bias parameters, state-dict keys and autograd path are preserved.
"""
from __future__ import annotations


def configure_rocm_patch_projection(model):
    import torch
    from torch import nn
    from torch.nn import functional as F

    if not torch.version.hip:
        return {"enabled":False,"reason":"not_rocm"}

    class PatchLinearProjection(nn.Module):
        def __init__(self, convolution):
            super().__init__()
            self.weight=convolution.weight
            self.bias=convolution.bias
            self.kernel_size=convolution.kernel_size
            self.in_channels=convolution.in_channels
            self.out_channels=convolution.out_channels
            self._blink_rocm_patch_projection=True

        def forward(self, value):
            if value.ndim!=5 or tuple(value.shape[1:])!=(self.in_channels,*self.kernel_size):
                raise ValueError("Qwen patch input differs from one non-overlapping kernel")
            result=F.linear(value.reshape(value.shape[0],-1),self.weight.reshape(self.out_channels,-1),self.bias)
            return result.reshape(value.shape[0],self.out_channels,1,1,1)

    changed=[]
    for name,module in list(model.named_modules()):
        if not name.endswith("visual.patch_embed.proj"):
            continue
        if getattr(module,"_blink_rocm_patch_projection",False):
            changed.append(name);continue
        if not isinstance(module,nn.Conv3d) or module.kernel_size!=module.stride or module.groups!=1 \
                or module.padding!=(0,0,0) or module.dilation!=(1,1,1) or module.padding_mode!="zeros":
            raise ValueError("Unsupported Qwen patch projection; do not change its semantics")
        parent_name,attribute=name.rsplit(".",1)
        parent=model.get_submodule(parent_name)
        setattr(parent,attribute,PatchLinearProjection(module));changed.append(name)
    if not changed:
        raise ValueError("Qwen visual patch projection was not found")
    return {"enabled":True,"modules":changed,"method":"exact single-patch convolution as linear projection"}
