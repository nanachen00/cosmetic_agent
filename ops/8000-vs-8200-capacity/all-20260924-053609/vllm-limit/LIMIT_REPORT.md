# vLLM :8200 Current-Configuration Capacity Limit

- Capacity statement: **40 VU**
- Max sustainable measured VU: **40**
- First unsustainable VU above boundary: **41**
- Peak completion throughput: **161.0886 tok/s @ VU 44**
- Current max-num-seqs: **8**
- Current gpu-memory-utilization: **0.3**
- Conservative startup caps detected: **True**

This is the limit of the currently running vLLM configuration under the configured SLO.
It is not labeled as the absolute MI300X hardware limit because this script deliberately does not restart/tune :8200.
