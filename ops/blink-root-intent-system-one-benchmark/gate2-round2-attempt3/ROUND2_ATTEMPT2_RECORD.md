# Gate2 Round2 Attempt2 Record

## Identity

- Run ID: `20261002-162547-2218624`
- Internal execution: `20261002-162714-6974d1c9`
- Gate1: `PASS`, `advance_allowed=true`
- Gate2 strict result: `BLOCKED`, `advance_allowed=false`

The uploaded result contains Gate1 evidence and a passing Gate1 status, but does not include a separate `GATE1_REUSE_PROVENANCE.json`; therefore this record does not claim independent proof of how that Gate1 directory was populated.

## Formal result

| Arm | Formal rows | Full-path pass | Status |
|---|---:|---:|---|
| A | 39 | 13 | FAIL / incomplete |
| B | 52 | 49 | FAIL |
| C | 52 | 39 | FAIL |

Arm A is not comparable as a 52-request arm: its `product_search` formal batches were skipped because the formal-phase smoke request itself hit `finish_reason=length` at the 240-token limit.

## Request-ID collision remained

Round2 v1 successfully generated unique `provider_request_id` values at the compat gateway and preserved the parent application request for audit. However the proxy process's `shadow_guard.py` subsequently rewrote outbound `X-Request-ID` to the parent application request ID before calling `:8200`.

A direct evidence pattern is:

- parent application request: `shadow-da407d5e402d4ed88ee5c50c502a5e8f`
- separate provider calls: `shadow-62a4201b9d2847f2b2dcc90b91c03db9`, `shadow-0d12d9ddd1314b44b2e81962c5d3095c`
- vLLM/LiteLLM error still references: `chatcmpl-shadow-da407d5e402d4ed88ee5c50c502a5e8f already running`

33 distinct parent request IDs showed the same collision signature:

- A / auto_root: 12
- A / kev_probe: 12
- C / kev_probe: 9

This is a Shadow harness correlation/egress bug, not a reason to modify BLINK team production logic.

## Generation truncation is a separate unresolved issue

Five product-search paths hit the exact caller ceiling:

- A smoke: 1
- B formal: 3
- C formal: 1

For all five:

- requested max tokens = 240
- completion tokens = 240
- finish reason = `length`

Attempt3 deliberately does **not** change this budget. Request-ID propagation is corrected first, then a new formal run determines whether token-budget treatment needs its own isolated remediation.

## Integrity

- strict smoke audit: 4/4 verified for A, B and C
- manifest sealed: 76 files
- manifest verification: PASS
- driver errors: empty
- cleanup: PASS

Global deployment observation reported `DEPENDENCY_DRIFT`, so performance comparison remained ineligible.

## Verdict

`BLOCKED`

This run is retained as diagnostic baseline evidence. It must not be relabeled as Gate2 PASS or as a complete 156-request comparison.
