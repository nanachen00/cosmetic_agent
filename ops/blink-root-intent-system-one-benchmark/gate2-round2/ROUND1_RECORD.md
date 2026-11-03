# BLINK Intent Parse Router — Gate2 Round 1 Record

Date: 2026-10-02  
Run ID: `20261002-075524-1298500`  
Internal execution: `20261002-094837-713e41de`

## Authoritative review verdict

`FAIL / advance_allowed=false`

Round 1 **completed the full experiment and integrity checks**, but did **not** satisfy the original Gate8 functional acceptance. The generated outer `GATE_STATUS.json` incorrectly promoted `experiment_complete=true` to `PASS` even though `functional_outcome_all_pass=false`.

This record preserves that distinction permanently:

- Experiment execution: COMPLETE
- Integrity / cleanup / request-audit checks: PASS
- Functional acceptance: FAIL
- Gate2 advance: BLOCKED

## Formal results

| Arm | Full-path PASS | Total | Pass rate | Formal benchmark |
|---|---:|---:|---:|---|
| A — current path | 22 | 52 | 42.31% | FAIL |
| B — Parser + Kev | 51 | 52 | 98.08% | FAIL |
| C — Parser-only | 38 | 52 | 73.08% | FAIL |

Total formal requests: **156**.

## Key findings

### Arm B

B was closest to acceptance: **51/52**. Its only failing formal request was:

- request: `shadow-1b46a387f10248b1974796db77403e8f`
- scenario: `product_search`
- VU: `8`
- failure: `no_truncated_generation`

The observed generation reached its configured token ceiling and ended with a length/truncation condition. This remains a real failure; Round 2 must not waive it merely to obtain PASS.

### Arm A

A had major failures in the current intent fallback path, including `expected_terminal_stage`, `no_stage_errors`, `no_llm_http_errors`, `not_known_failure_reply`, and native-schema/provider-path failures. It also had generation truncations.

### Arm C

C successfully exercised Parser direct bypass for the normal `auto_root` cases, but `kev_probe` cases necessarily returned to the current fallback path and failed there. C also had one generation truncation.

### Audit remediation

The new request-audit rules passed their self-tests. In particular, incomplete/truncated/tampered audit evidence, non-zero treatment decisions, and non-zero Kev attempts correctly block the special Product continuation rule.

## Round 2 acceptance rule

Gate2 may advance only when **both** of these are true:

1. experiment/integrity completion passes; and
2. functional acceptance passes.

Functional acceptance requires the original standard:

- every arm's smoke business checks pass;
- every arm's formal benchmark passes;
- every arm has exactly 52 formal requests;
- every arm has 52/52 full-path PASS;
- strict request-audit requirements remain satisfied.

`experiment_complete` alone is never sufficient for `status=PASS` or `advance_allowed=true`.

## Frozen non-remediation rules

Round 2 must not obtain PASS by:

- treating A/C failures as exempt comparison-arm outcomes;
- waiving truncation failures;
- dropping scenarios or requests;
- reducing 52 requests per arm;
- hiding failing rows;
- modifying production/team service logic merely to make the test pass.
