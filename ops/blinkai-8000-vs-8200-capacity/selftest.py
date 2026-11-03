#!/usr/bin/env python3
import compare_8000_8200_multiturn as m

assert m.parse_vus("1,2,4,2048") == [1,2,4,2048]

try:
    m.parse_vus("4096")
except ValueError:
    pass
else:
    raise AssertionError("4096 VU should be rejected")

good={
  "skipped":False,
  "request_success_rate":1.0,
  "conversation_completion_rate":1.0,
  "latency_s":{"p95":2.0},
}
assert m.sustainable(good,15,.99,.95)

bad={
  "skipped":False,
  "request_success_rate":.90,
  "conversation_completion_rate":1.0,
  "latency_s":{"p95":2.0},
}
assert not m.sustainable(bad,15,.99,.95)

print("PASS")
