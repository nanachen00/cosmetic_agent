# :8000 vs :8200 Multi-VU Multi-Turn Capacity

SLO: success ≥ 99%, conversation completion ≥ 95%, p95 ≤ 15.0s

| VU | Blue success | Green success | Blue RPS | Green RPS | RPS gain | Blue tok/s | Green tok/s | tok/s gain | Blue p95 | Green p95 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 100.0% | 100.0% | 0.579 | 0.533 | 0.92× | 17.60 | 16.71 | 0.95× | 3.694 | 3.841 |
| 2 | 83.3% | 100.0% | 0.727 | 1.577 | 2.17× | 17.89 | 48.08 | 2.69× | 3.636 | 2.576 |
| 4 | 70.0% | 100.0% | 0.966 | 3.184 | 3.30× | 17.39 | 97.11 | 5.58× | 3.372 | 2.551 |
| 8 | 61.1% | 100.0% | 1.464 | 4.945 | 3.38× | 17.83 | 155.14 | 8.70× | 3.081 | 3.191 |

- Blue sustainable max VU: **1**
- Green sustainable max VU: **8**
- Sustainable-capacity gain: **8.0×**
- Blue peak completion throughput: **17.89 tok/s**
- Green peak completion throughput: **155.14 tok/s**
- Peak completion-throughput gain: **8.6713×**

Higher VU than the active sequence limit measures scheduler/queue/backpressure capacity, not simultaneous decoding of every VU.
