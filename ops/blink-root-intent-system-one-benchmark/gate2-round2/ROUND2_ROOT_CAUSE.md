# Gate2 Round 2 root-cause split

Round 1 exposed **two separate classes of failure**. They must not be fixed by the same mechanism.

## 1. Gate verdict regression — harness bug

The outer Gate2 verdict used `experiment_complete` as the effective PASS condition even though `functional_outcome_all_pass=false`.

Round 2 fixes this with `strict_gate2_finalize.py`. Experiment completion is retained as evidence, but it cannot advance the gate.

## 2. A/C HTTP 400 failures — Shadow provider request-ID collision

Round 1 raw logs show repeated provider responses such as:

```text
Request id chatcmpl-shadow-... already running.
```

The reviewed Shadow compatibility gateway forwarded the **same application request ID** as `X-Request-ID` for every model call belonging to that application request. When multiple calls overlapped, the provider treated them as the same active generation and rejected the later call with HTTP 400.

This is a Shadow harness correlation bug, not a reason to change BLINK team application logic.

Round 2 changes only the Shadow bridge:

```text
application request ID
    -> retained as parent/audit correlation ID

per-model-call provider request ID
    -> fresh shadow-<uuid> for every provider call
```

The proxy receives both:

```text
X-Request-ID: <unique provider-call id>
X-Blink-Shadow-Parent-Request-ID: <original application request id>
X-Blink-Shadow-Call-ID: <existing gateway call id>
```

Audit and path correlation continue to use the original application request ID. The vLLM-facing request ID becomes unique per actual model call.

## 3. B arm truncation — real functional outcome, not patched here

B reached 51/52. The remaining failure was:

```text
request_id: shadow-1b46a387f10248b1974796db77403e8f
scenario: product_search
VU: 8
requested_max_tokens: 240
completion_tokens: 240
finish_reason: length
failure: no_truncated_generation
```

Round 2 **does not raise the token limit in the harness** and does not waive this failure. It remains a service-level outcome to diagnose after the request-ID collision is removed, because changing the limit merely to obtain PASS would alter the experiment condition.
