# Prism verification report

Generated 2026-11-12 20:03 UTC by `python -m tools.verify` on Windows-11-10.0.26200-SP0, Python 3.14.7.
Every number below was produced by that run, against a fresh private stack: an empty temporary database,
the two mock providers and the gateway. Nothing is copied from an earlier run.

## Summary

| # | Check | Result | Detail |
|---|---|---|---|
| 1 | Automated tests | **PASS** | 293 passed in 19.97s |
| 2 | Provided smoke test | **PASS** | 17 passed, 1 warnings, 0 failed |
| 3 | Budget rejection | **PASS** | first request 200, second 402 budget_exceeded |
| 4 | Provider failover | **PASS** | down, recovery and slow provider all handled |
| 5 | Load test: no over-admission | **PASS** | 10 accepted of a limit of 10, 20 rate limited, 0 other, out of 30 |
| 6 | Accounting: usage API equals the client's totals | **PASS** | requests, tokens and cost reconcile |
| 7 | Latency summary | **info** | gateway median 48 ms total, 12 ms overhead |
| 8 | Cache tenant isolation and persistence | **PASS** | another key got a real miss, and the owner's entry survived a restart |
| 9 | Routing eval (auto) | **PASS** | the router beats the length-only baseline on both sets |
| 10 | Cache matcher eval | **PASS** | no false hits at the strictest threshold (0.92) |

**9 of 9 graded checks passed.**

## Details

### 1. Automated tests  (PASS)

293 passed in 19.97s

```text
........................................................................ [ 24%]
........................................................................ [ 49%]
........................................................................ [ 73%]
........................................................................ [ 98%]
.....                                                                    [100%]
293 passed in 19.97s
```

### 2. Provided smoke test  (PASS)

17 passed, 1 warnings, 0 failed

```text

[1] Non-streaming completion + header contract
  PASS  returns 200  (got 200)
  PASS  body has choices and usage
  PASS  header x-prism-provider present  (alpha/alpha-small)
  PASS  header x-prism-cache present  (miss)
  PASS  header x-prism-cost-usd present  (0.000013500)
  PASS  first request is a cache miss  (miss)

[2] Streaming
  PASS  returns 200  (got 200)
  PASS  content-type is text/event-stream  (text/event-stream; charset=utf-8)
  PASS  received multiple SSE chunks  (19 data lines)
  PASS  stream ends with [DONE]

[3] Auth
  PASS  invalid key rejected with 401/403  (got 401)

[4] Unknown model
  PASS  unknown model rejected with 4xx  (got 404)
  PASS  error body explains the problem  ({'error': {'message': "Unknown model or alias 'no-such-model-xyz'", 'type': 'not)

[5] Semantic cache: exact repeat
  PASS  identical repeat is a cache hit  (hit)

[6] Semantic cache: paraphrases (each pair soft; at least one pair must hit)
  PASS  pair A paraphrase is a cache hit  (hit)
  WARN  pair B paraphrase is a cache hit  (miss)
  PASS  matching is semantic, not exact-only (at least one pair hit)

[7] Semantic cache: unrelated prompt
  PASS  unrelated prompt is a cache miss  (miss)

==================================================
  17 passed, 1 warnings, 0 failed
```

### 3. Budget rejection  (PASS)

first request 200, second 402 budget_exceeded

```text
request 1: HTTP 200, cost header 0.000016350 (the key's whole budget is $0.00001)
request 2: HTTP 402
{"message": "Monthly budget of $0.00001 exhausted for this key", "type": "budget_exceeded", "code": "budget_exceeded"}
```

### 4. Provider failover  (PASS)

down, recovery and slow provider all handled

```text
alpha down        -> HTTP 200, served by beta/beta-small, fallback true  [ok]
alpha restored    -> HTTP 200, served by alpha/alpha-small, fallback false  [ok]
alpha 3000 ms slow -> HTTP 200, served by beta/beta-small, fallback true, answered in 2.03 s (timeout 2 s)  [ok]
```

### 5. Load test: no over-admission  (PASS)

10 accepted of a limit of 10, 20 rate limited, 0 other, out of 30

```text
Bursting 30 requests at concurrency 10 ...

Burst finished in 0.3s
  accepted (200):      10
  rate limited (429):  20
  latency avg/p95 ms:  257 / 312

Client-side accounting totals - compare with your usage API for this key:
  accepted requests:   10
  prompt tokens:       120
  completion tokens:   256
  sum of cost headers: 0.000172 USD (10 of 10 had a numeric header)

Over-admission check OK: 10 accepted <= limit 10.
```

### 6. Accounting: usage API equals the client's totals  (PASS)

requests, tokens and cost reconcile

```text
requests           client       10   usage API       10   match
prompt tokens      client      120   usage API      120   match
completion tokens  client      256   usage API      256   match
cost (USD)         client 0.000172   usage API 0.000172   match (exact: 171600 nano-USD)
```

### 7. Latency summary  (info)

gateway median 48 ms total, 12 ms overhead

```text
                                    min  median    p95    max   (ms, 10 accepted requests)
gateway total (latency_ms)           44      48     52     52
waiting on provider (upstream_ms)     30      35     38     38
gateway overhead (difference)         8      12     21     21
load-test script, its own view              257    312   (avg and p95; includes client-side start-up)
```

### 8. Cache tenant isolation and persistence  (PASS)

another key got a real miss, and the owner's entry survived a restart

```text
search key, first ask            -> cache miss
search key, repeat               -> cache hit
(gateway restarted: memory cleared, database kept)
search key, after the restart    -> cache hit   (the entry persisted)
free-tier key, identical prompt  -> HTTP 200, cache miss, cost 0.000014250, served by alpha/alpha-small
free-tier key, repeat            -> cache hit   (its own entry now)
cache entries per key            -> search 7, free-tier 11
```

### 9. Routing eval (auto)  (PASS)

the router beats the length-only baseline on both sets

```text
provided  set (20 cases): router 20/20 = 100.0%   length-only baseline 15/20 = 75.0%
held-out  set (40 cases): router 35/40 = 87.5%   length-only baseline 26/40 = 65.0%
   MISS hold_021: expected smart, got fast: 'A bat and a ball cost $1.10 in total. The bat costs $1 more than the ball. How m'
   MISS hold_022: expected smart, got fast: 'Is it ethical for a self-driving car to prioritize its passengers over pedestria'
   MISS hold_032: expected smart, got fast: 'A train leaves Chicago at 3pm going 60 mph. Another leaves New York at 4pm going'
   MISS hold_038: expected smart, got fast: 'Explain the CAP theorem and how it applies to choosing between a CP and an AP da'
   MISS hold_040: expected smart, got fast: 'Three boxes are labeled apples, oranges, and mixed, but every label is wrong. Yo'
(the baseline is 'smart if the prompt has at least 46 characters', the best threshold on the provided set)
```

### 10. Cache matcher eval  (PASS)

no false hits at the strictest threshold (0.92)

```text
threshold 0.92: paraphrases hit 3/26 = 11.5%, false hits 0/26 = 0.0%
threshold 0.85: paraphrases hit 5/26 = 19.2%, false hits 2/26 = 7.7%  (n08, n25)
```

## Reading the latency numbers

`latency_ms` in the request log is the time inside the gateway and `upstream_ms` is the part spent
waiting on the provider; their difference is what the gateway itself adds. The provided load-test script
prints a larger figure because it also counts connection setup and one-time client start-up on each of
its threads, so judge the gateway by the gateway's own numbers.

## What this does not show

- The providers are the mock providers. Real provider latency, rate limits and failure modes will differ.
- One machine, one gateway process. The rate limiter and provider health are in memory, so behaviour
  with several instances is not covered.
- The routing and cache labels are the author's judgment, and the rules were tuned after seeing the first
  results (the README says which numbers are independent and which are not).
- The eval sets are small (20 and 40 prompts, 52 cache pairs): accuracy figures carry real uncertainty.
