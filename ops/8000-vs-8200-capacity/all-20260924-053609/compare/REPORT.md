# :8000 vs :8200 Multi-VU Multi-Turn Capacity

SLO: success ≥ 99%, conversation completion ≥ 95%, p95 ≤ 15.0s

| VU | Blue success | Green success | Blue RPS | Green RPS | RPS gain | Blue tok/s | Green tok/s | tok/s gain | Blue p95 | Green p95 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 100.0% | 100.0% | 0.608 | 0.530 | 0.87× | 18.30 | 16.66 | 0.91× | 3.537 | 3.858 |
| 2 | 83.3% | 100.0% | 0.747 | 1.511 | 2.02× | 18.37 | 47.30 | 2.57× | 3.606 | 2.582 |
| 4 | 70.0% | 100.0% | 0.993 | 3.002 | 3.02× | 18.01 | 94.29 | 5.24× | 3.890 | 2.560 |
| 8 | 61.1% | 100.0% | 1.490 | 4.942 | 3.32× | 18.42 | 154.15 | 8.37× | 3.025 | 3.193 |
| 16 | SKIP | 100.0% | NA | 5.034 | NA× | NA | 158.65 | NA× | NA | 6.298 |
| 32 | SKIP | 100.0% | NA | 5.020 | NA× | NA | 158.41 | NA× | NA | 12.611 |
| 64 | SKIP | 100.0% | NA | 5.067 | NA× | NA | 160.95 | NA× | NA | 24.174 |
| 128 | SKIP | 86.0% | NA | 5.873 | NA× | NA | 157.06 | NA× | NA | 27.421 |
| 256 | SKIP | 70.0% | NA | 7.644 | NA× | NA | 145.65 | NA× | NA | 27.401 |
| 512 | SKIP | 59.9% | NA | 11.588 | NA× | NA | 136.45 | NA× | NA | 25.115 |
| 1024 | SKIP | SKIP | NA | NA | NA× | NA | NA | NA× | NA | NA |
| 2048 | SKIP | SKIP | NA | NA | NA× | NA | NA | NA× | NA | NA |

- Blue sustainable max VU: **1**
- Green sustainable max VU: **32**
- Sustainable-capacity gain: **32.0×**
- Blue peak completion throughput: **18.42 tok/s**
- Green peak completion throughput: **160.95 tok/s**
- Peak completion-throughput gain: **8.7379×**

Higher VU than the active sequence limit measures scheduler/queue/backpressure capacity, not simultaneous decoding of every VU.
