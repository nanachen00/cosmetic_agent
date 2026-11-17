"""Run in the ROCm training environment; CPU reference, GPU implementation."""
import copy
import unittest

import torch
from torch import nn

from rocm_patch_projection import configure_rocm_patch_projection


class ProjectionTests(unittest.TestCase):
    def model(self):
        model=nn.Module();model.visual=nn.Module();model.visual.patch_embed=nn.Module()
        model.visual.patch_embed.proj=nn.Conv3d(3,6,(2,4,4),stride=(2,4,4),bias=True).double()
        return model

    def test_cpu_forward_and_gradients_equal_original_convolution(self):
        torch.manual_seed(42);reference=self.model();candidate=copy.deepcopy(reference)
        before={name:value.clone() for name,value in candidate.state_dict().items()}
        configure_rocm_patch_projection(candidate)
        x=torch.randn(5,3,2,4,4,dtype=torch.float64,requires_grad=True)
        y=x.detach().clone().requires_grad_(True)
        expected=reference.visual.patch_embed.proj(x);actual=candidate.visual.patch_embed.proj(y)
        torch.testing.assert_close(actual,expected,rtol=1e-12,atol=1e-12)
        actual.square().sum().backward();expected.square().sum().backward()
        torch.testing.assert_close(y.grad,x.grad,rtol=1e-12,atol=1e-12)
        for (name,a),(_,b) in zip(candidate.named_parameters(),reference.named_parameters()):
            torch.testing.assert_close(a.grad,b.grad,rtol=1e-12,atol=1e-12)
            torch.testing.assert_close(a.detach(),before[name])
        self.assertEqual(set(before),set(candidate.state_dict()))
        configure_rocm_patch_projection(candidate)

    def test_bf16_gpu_projection_and_backward(self):
        model=self.model().float();reference=copy.deepcopy(model)
        configure_rocm_patch_projection(model);model=model.to(device='cuda',dtype=torch.bfloat16)
        x=torch.randn(5,3,2,4,4).to(torch.bfloat16).float()
        expected=reference.visual.patch_embed.proj(x)
        actual=model.visual.patch_embed.proj(x.to('cuda',dtype=torch.bfloat16))
        torch.testing.assert_close(actual.float().cpu(),expected,rtol=.03,atol=.03)
        actual.float().square().sum().backward()
        self.assertTrue(torch.isfinite(model.visual.patch_embed.proj.weight.grad).all())

    def test_rejects_overlapping_or_padded_convolution(self):
        model=self.model();model.visual.patch_embed.proj.padding=(1,1,1)
        with self.assertRaises(ValueError):configure_rocm_patch_projection(model)


if __name__=='__main__':unittest.main()
