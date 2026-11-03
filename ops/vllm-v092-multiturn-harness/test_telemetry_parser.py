#!/usr/bin/env python3
from benchmark_multiturn_chat import parse_amd_smi_monitor

samples = [
"""GPU  POWER   GPU_T   MEM_T   GFX_CLK   GFX%   MEM%   ENC%   DEC%      VRAM_USAGE
  0  174 W   39 °C   35 °C  2099 MHz    0 %    0 %    N/A    0 %   85.2/192.0 GB
""",
"""GPU  XCP    POWER    GPU_T    MEM_T   GFX_CLK    GFX%    MEM%    ENC%    DEC%   VRAM_USED   VRAM_TOTAL
  0    0    110 W    47 °C    39 °C   210 MHz     0 %     0 %   N/A      0 %      14 MB     96432 MB
"""
]

a = parse_amd_smi_monitor(samples[0], 0)
assert a and a["vram_used_gb"] == 85.2 and a["vram_total_gb"] == 192.0 and a["power_w"] == 174.0, a

b = parse_amd_smi_monitor(samples[1], 0)
assert b and b["xcp"] == 0 and abs(b["vram_used_gb"] - 14/1024) < 1e-9, b
assert abs(b["vram_total_gb"] - 96432/1024) < 1e-9, b

print("PASS")
