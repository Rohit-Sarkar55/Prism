# Prism: LLM gateway and semantic cache

Prism is an OpenAI-compatible gateway that sits between applications and model providers. One API in
front of several providers, with the infrastructure every AI product ends up needing: virtual keys,
per-team limits and budgets, retries and failover, difficulty-based routing (`auto`), token-by-token
streaming, a semantic cache, exact cost metering, a complete request log, and an ops console.

It runs against two **mock providers** (`alpha`, `beta`) that ship with the course pack, so everything
here is free and reproducible. Providers sit behind an adapter, so real ones can be added without
touching the core.

## Contents
[Quick start](#quick-start) · [Demo keys](#seeded-demo-keys) · [Reproduce the demo](#reproduce-the-demo) · [API](#api) · [Architecture](#architecture) ·
[How it works](#how-it-works) · [Configuration](#configuration) · [Verification](#verification) ·
[Known limitations](#known-limitations) · [Windows notes](#windows-notes)

## Quick start

Needs Python 3.11 or newer (developed on 3.14). From the project folder (the one containing `app/`,
`tests/` and `data/`):

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1                 # macOS/Linux: source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

Start the two mock providers and the gateway with one command:

```powershell
python -m tools.dev_stack              # add --fresh-db to delete prism.db first (a clean slate)
```

It waits until all three answer `/health`, prints the console URL, writes each process's output to
`.prism-logs/`, and stops everything on Ctrl+C. It refuses to start if a port is already taken, which is what
a stack left running from an earlier session looks like (it would serve old code and old config). If you
prefer separate terminals, run these three, each with the venv active:

```powershell
python scripts\mock_provider.py --port 9001 --name alpha
python scripts\mock_provider.py --port 9002 --name beta
python -m uvicorn app.main:app --port 8080
```

On first start the gateway creates `prism.db` (SQLite) and loads the price table, the four seeded
tenants and the routing config from `data/`. Then:

```bash
curl -si http://127.0.0.1:8080/v1/chat/completions \
  -H "Authorization: Bearer prism-sk-search-1a2b3c" -H "Content-Type: application/json" \
  -d '{"model":"fast","messages":[{"role":"user","content":"What is a message queue?"}]}'
```

You get an OpenAI-shaped completion plus the `x-prism-*` headers. Open
`http://127.0.0.1:8080/console` for the ops console and enter the admin token. On PowerShell, put the JSON
in a file and use `curl.exe -d "@body.json"`, because PowerShell mangles inline JSON quotes.

## Seeded demo keys

These come from `data/seed_keys.json` and are loaded unchanged on every start (usage is never reset by a restart).

| Team | Key | Models | Limit | Monthly budget | Cache |
|---|---|---|---|---|---|
| search | `prism-sk-search-1a2b3c` | fast | 60 rpm | $50 | on, threshold 0.92 |
| research | `prism-sk-research-4d5e6f` | fast, smart, auto | 300 rpm | $500 | off |
| free-tier | `prism-sk-free-7g8h9i` | fast | 10 rpm | $5 | on, threshold 0.85 |
| budget-demo | `prism-sk-budget-demo-0j1k2l` | fast | 60 rpm | $0.00001 | off |

`budget-demo` is for showing budget rejection (one request spends more than its whole budget);
`free-tier` is for showing the rate limit. The admin token is `demo-admin-token` unless you set
`PRISM_ADMIN_TOKEN`. Virtual keys are stored in plain text: the seed values are public demo credentials
and the scripts need them as given.

## Reproduce the demo

Everything runs locally against the mock providers. In one terminal, start the stack on a clean database; in
another, run the narrated demo:

```powershell
python -m tools.dev_stack --fresh-db     # terminal 1: leave it running
python -m tools.demo                     # terminal 2: press Enter between steps; add --no-pause to run straight through
```

Open `http://127.0.0.1:8080/console` next to it (admin token `demo-admin-token`) and watch it fill in. The demo
walks through six things, in this order, and prints the evidence for each:

1. **Streaming**: tokens arrive one by one (time to first token, chunk count, `[DONE]`), then the log row with its cost.
2. **Smart routing**: `auto` sends an easy question to the cheap model, a hard one to the strong model, and a long
   input with a trivial question to the cheap model, each with the reason it was logged.
3. **Semantic cache**: a repeat is a free hit, a paraphrase is a hit, a look-alike with a different intent is a
   miss, and the same prompt from another team is a real, paid miss.
4. **Budget rejection**: the `budget-demo` key gets one request, then a 402.
5. **Rate limiting**: the `free-tier` key is cut off with 429 and `Retry-After` at its 10 requests per minute.
6. **Live failover**: `alpha` is taken down, then made 3 seconds slow, while the gateway runs; `beta` serves with
   `x-prism-fallback: true`, and `alpha` takes over again once restored.

The demo needs an unused database (an unspent budget, a free rate-limit window), so restart the stack with
`--fresh-db` before each run. It checks this first and tells you what to do if the database is not clean.

## API

### `POST /v1/chat/completions`

OpenAI-compatible. Authenticate with `Authorization: Bearer <virtual key>`.

| Field | Notes |
|---|---|
| `model` | an alias (`fast`, `smart`, `auto`) or a concrete model name; it must be on the key's allowlist exactly as sent |
| `messages` | non-empty list of `{role, content}` |
| `stream` | `true` for server-sent events, see [Streaming](#streaming) |
| `temperature`, `top_p`, `max_tokens`, `stop`, `seed`, `presence_penalty`, `frequency_penalty` | forwarded to the provider; every other field is dropped |

Aliases (from `data/gateway_config.sample.json`): `fast` is `alpha-small` with `beta-small` as fallback,
`smart` is `alpha-large` then `beta-large`, `auto` picks `fast` or `smart` per prompt.

**Response headers** (on every data-plane response, errors included):

| Header | Meaning |
|---|---|
| `x-prism-provider` | `alpha/alpha-small` style: who served it. `cache` on a cache hit, `none` on a rejection |
| `x-prism-cache` | `hit` or `miss` |
| `x-prism-fallback` | `true` only when a model other than the primary served it |
| `x-prism-cost-usd` | exact cost with 9 decimals, `0` for cache hits and rejections. Omitted on streams (the cost is not known when headers are sent) |
| `x-prism-request-id` | matches the `request_id` in the request log |

**Errors** use the OpenAI shape `{"error": {"message", "type", "code"}}`. Each rejection is distinct:

| Status | `type` | When | Logged as |
|---|---|---|---|
| 401 | `authentication_error` | missing, malformed, unknown or disabled key | `rejected_auth` |
| 400 | `invalid_request_error` | body is not valid JSON or lacks `model` / `messages` | `rejected_invalid` |
| 404 | `not_found_error` | unknown model or alias | `rejected_not_found` |
| 403 | `model_not_allowed` | the key's allowlist does not include it | `rejected_allowlist` |
| 429 | `rate_limit_exceeded` | over the key's requests per minute; `Retry-After` header | `rejected_rate_limit` |
| 402 | `budget_exceeded` | the key's monthly budget is spent | `rejected_budget` |
| 502 | `upstream_error` | every provider in the chain failed; no provider details are exposed | `upstream_error` |

Checks run in this order: authenticate, validate, resolve model, allowlist, rate limit, budget, cache,
upstream. A typo is a 404 ("unknown model"), not a 403, because resolution comes before authorization.

### Admin API and console

Admin routes need `Authorization: Bearer <admin token>`. A tenant key does not work here.

| Route | Purpose |
|---|---|
| `GET /admin/keys` | every key with limits, this month's spend and budget used (%) |
| `GET /admin/usage?key=...&from=...&to=...` | totals for a key over a date range (default: this month): requests, tokens, `cost_usd`, exact `cost_nano`, cache hits |
| `GET /admin/logs?key=...&limit=...` | recent request-log entries, newest first (default 50, max 500) |
| `GET /admin/cache/stats?key=...` | hits, misses, hit rate, entries, money saved; overall and per key |
| `DELETE /admin/cache?key=...` | purge the cache, for one key or all |
| `GET /admin/providers` | provider health from recent traffic |
| `GET /health` | liveness, no auth |
| `GET /console` | the ops console page (loads without a token; you type the token in) |

Each log entry holds: `request_id`, `virtual_key`, `requested_model`, `resolved_provider`, `resolved_model`,
`status`, `prompt_tokens`, `completion_tokens`, `cost_nano`, `cache` (`hit`, `miss` or `bypass`),
`cache_similarity`, `fallback`, `route_reason`, `retries`, `latency_ms` (total time in the gateway),
`upstream_ms` (time waiting on the provider) and `created_at`. Bodies are never logged.

## Architecture

```
client ──▶ authenticate ─▶ validate ─▶ resolve alias ─▶ allowlist ─▶ rate limit ─▶ budget
                                          │ (auto: difficulty router)                  │
                                          ▼                                            ▼
                                      cache lookup ── hit ──▶ response ◀──── upstream chain
                                          │ miss                                  ▲   │
                                          └──────▶ retries ▶ failover ▶ provider adapter ─▶ alpha / beta
                                                       ▲ circuit breaker
              every outcome ──▶ one transaction: request log row + monthly usage (SQLite, WAL)
```

| Module | Responsibility |
|---|---|
| `app/routes/chat.py` | the data-plane pipeline above |
| `app/routes/admin.py`, `app/routes/console.py`, `app/static/console.html` | admin API and the console |
| `app/routing.py`, `app/difficulty.py` | alias resolution and the `auto` difficulty classifier |
| `app/upstream.py`, `app/health.py` | retries, backoff, failover, circuit breaker |
| `app/providers/` | adapter interface, the OpenAI-compatible HTTP adapter, registry, an in-process fake for tests |
| `app/streaming.py` | SSE pass-through and its accounting |
| `app/cache.py`, `app/embedding.py` | the semantic cache and its matcher |
| `app/ratelimit.py`, `app/costs.py`, `app/money.py` | rate limiter, cost computation, integer money |
| `app/store.py`, `app/db.py`, `app/seed.py` | persistence, schema and data loading |
| `tools/routing_eval.py`, `tools/cache_eval.py` | one-command evals for the two AI decisions |
| `tools/dev_stack.py` | starts the mock providers and the gateway together and stops them cleanly |
| `tools/verify.py` | runs every verification check against a private stack and writes `VERIFICATION.md` |
| `tools/demo.py` | the narrated demo for the video: streaming, routing, cache, budget, rate limit, failover |

State is SQLite in WAL mode: `virtual_keys`, `usage_monthly`, `request_logs`, `cache_entries`. Price table,
providers and aliases are loaded from `data/` at startup. Rate-limiter and provider-health state live in memory.

## How it works

### Keys, limits and budgets
- **Rate limit:** a sliding-window log per key (never more than `rpm` requests in any 60 seconds). The
  check and the record are one synchronous step with no `await` between them, so concurrent requests cannot
  all see "9 of 10 used" and proceed. Rejected requests are not recorded, and allowlist rejections do not
  use up slots. State is per process and resets on restart.
- **Budget:** a request is admitted while `spent < budget`, and its real cost is added afterwards. The cost
  is only known after the provider answers, so a request admitted just before the budget runs out can
  overshoot it, and requests in flight at the same moment can each do so. The overshoot is bounded by the
  concurrent requests times one request's cost, and is shown in the console (a bar can pass 100%).
  Spend is read from `usage_monthly`, keyed by calendar month (UTC), so the budget resets by itself on the
  first request of a month.
- **402, not 429, for budgets:** SDKs retry 429 automatically, and retrying cannot fix an exhausted budget.

### Metering
- Cost comes from the provider-reported token usage and the price table, as **integer nano-USD**
  (price per million tokens times 1000 is nano-USD per token, exact for the table's prices). Floats drift
  when summed, and a client adding up cost headers must reconcile with the usage API.
- A served request's log row and its usage increment are written in **one transaction under one lock**, so
  the totals can never disagree with the log.
- A response without `usage` is treated as a provider failure, because cost depends on it.

### Routing and the `auto` alias
`fast` and `smart` resolve to a primary model plus an ordered fallback chain. `auto` classifies the prompt
and picks one of those two (`app/difficulty.py`):

1. It separates the instruction from any payload. Quoted text and long or data-like sentences (pasted logs,
   rosters, emails) are ignored, so a long input with a trivial question stays easy.
2. It scores the instruction with weighted signals: proofs, comparisons, design and planning, causal "why",
   estimation, multi-part demands and systems vocabulary push towards `smart`; lookups, conversion,
   translation, extraction and light rewriting push towards `fast`. Jargon alone can never reach the threshold.
3. A score of 2.0 or more is `smart`, otherwise `fast`. Below the threshold it picks the cheap tier on purpose.

Difficulty is a property of what the prompt asks for, not its length. The reason (`complex (score 4): ...`)
is logged per request. Why rules and not a model: the router runs on every `auto` request, so it must be
fast, free, deterministic and explainable. Only the last user message is judged.

### Retries and failover
| Failure | Behaviour |
|---|---|
| 5xx, network error | retry the same provider: up to 3 attempts, 200 ms then 400 ms backoff, +-25% jitter |
| connect error, timeout, 429, malformed reply | fail over to the next model immediately, no retry |
| 400, 413, 422 from the provider | stop: another provider would reject the same bad request |
| other 4xx from the provider | fail over (a provider-side configuration problem) |

Each attempt has its own timeout (2 s, connect 1 s), and one overall deadline (10 s) bounds retries plus
failover. When a fallback serves the request, `x-prism-fallback` is `true`, and the log records the serving
model, the retry count, and the cost priced as that model. If every provider fails the caller gets a generic
502.

A **circuit breaker** skips a provider that is clearly unhealthy: 5 consecutive failures, or an error rate
of 50% or more over at least 10 calls in 60 seconds. One probe request is allowed every 5 seconds and a
successful probe closes the circuit at once, so recovery takes seconds. The sample minimum stops a merely
flaky provider (say 30% errors) from being written off by chance.

### Streaming
- Chunks are forwarded the moment they arrive; nothing is collected first. The stream ends with
  `data: [DONE]`.
- The gateway waits for the **first chunk** before sending any HTTP response, so a provider failure up to
  that point is a clean 502 or a failover. After the first chunk there is no going back.
- If the provider dies mid-stream, the stream ends with an error event and **no** `[DONE]` (that marker
  means success), and the gateway never splices another provider's output onto it.
- Usage and cost come from the final chunk and are recorded exactly once, in a `finally` block with the
  cleanup shielded from cancellation, so a client that disconnects still gets accounted for.

### Semantic cache
- **Matcher:** a documented local substitute for an embedding model (`app/embedding.py`). It reduces a
  prompt to stemmed content words and compares them with cosine similarity, scaled down when the question
  type differs. The brief allows a documented substitute; swapping in a real embedding model only means
  providing the same two methods.
- **Guards that force a miss:** identifier-like tokens (anything with a digit, including the random salts the
  test scripts use) must match exactly; negation must agree (`with` is never `without`); the direction of
  `to`, `from`, `than`, `before` and `after` must match ("A faster than B" is not "B faster than A").
- **Scope:** an entry is only served to a request with the same virtual key, the same alias as sent, the same
  request parameters, and an exactly matching system prompt and earlier turns. Similarity is applied only to
  the last user message and can never bridge a scope boundary, so one tenant can never receive another's
  answer.
- **Never cached or looked up:** keys with the cache off, time-sensitive prompts ("current", "now", "latest",
  "weather", ...), prompts over 4000 characters, errors, partial streams.
- **Hits:** `x-prism-cache: hit`, cost 0, log status `cache_hit`, and usage tokens of 0, because usage means
  tokens consumed upstream. A hit still uses a rate-limit slot, and the budget is checked before the cache.
  A hit is replayed to a streaming caller as a proper SSE stream.
- **Lifecycle:** entries expire after 24 hours and each key keeps at most 500 (least recently used go
  first). Entries persist in SQLite across restarts. A broken cache lookup degrades to a miss, never an error.

### Ops console
One static page with no build step: usage by key with budget bars, cache stats, provider health and the 30
most recent requests. Request logs hold text that callers control (the model name they sent), so the page
builds its DOM from text nodes only (a test forbids `innerHTML` and friends) and the server sends a
per-request nonce Content-Security-Policy. The admin token is kept in `sessionStorage` and never in a URL.

## Configuration

Environment variables (or a `.env` file), all optional:

| Variable | Default | Meaning |
|---|---|---|
| `PRISM_DB_PATH` | `prism.db` in the project folder | SQLite file |
| `PRISM_DATA_DIR` | `data/` in the project folder | where the seed and config files are read from |
| `PRISM_ADMIN_TOKEN` | `demo-admin-token` | admin API token |
| `PRISM_PROVIDER_TIMEOUT_S` | `2.0` | per-attempt upstream timeout |
| `PRISM_UPSTREAM_DEADLINE_S` | `10.0` | overall limit for retries plus failover |
| `PRISM_CACHE_TTL_S` | `86400` | cache entry lifetime |
| `PRISM_CACHE_MAX_ENTRIES_PER_KEY` | `500` | cache size per key |

Retry counts, backoff, the circuit breaker's thresholds and the alias chains are in
`data/gateway_config.sample.json`. The timeout default is deliberately low so that the demo's "provider
becomes slow" case trips it; real providers usually need more. Real provider keys do not belong in that
file: it holds the mock providers' placeholder keys only.

## Verification

One command runs everything below and writes a report with the real numbers from your machine:

```powershell
python -m tools.verify                 # one to two minutes; stop any running dev_stack first (it needs the ports)
```

It starts its own stack with a temporary database (your `prism.db` is never touched) and runs the test suite,
the smoke test, a budget rejection, three failover drills (provider down, restored, and 3 seconds slow), the
load test with a reconciliation against the usage API, a cache isolation and restart check, and both evals.
Then it writes `VERIFICATION.md` with a pass or fail summary and the raw output of every step. Run it on your
own machine: a report generated elsewhere describes that machine. The individual commands are:

```powershell
python -m pytest                       # the whole suite
python -m ruff check .
python scripts\smoke_test.py --url http://127.0.0.1:8080 --key prism-sk-search-1a2b3c --model fast
python scripts\load_test.py  --url http://127.0.0.1:8080 --key prism-sk-free-7g8h9i --model fast --requests 30 --concurrency 10 --rpm-limit 10
python -m tools.routing_eval           # the auto router against the labeled cases
python -m tools.cache_eval             # the cache matcher against labeled pairs
```

- **Tests** cover cost arithmetic, the rate limiter (including a threaded test that must admit exactly the
  limit), allowlists, budgets, routing, retries and failover, streaming, cache decisions and tenant isolation,
  and the admin API. Provider calls are tested against an in-process fake and against the real mock provider.
- **Smoke test:** all sections pass. Pair B (`money back` vs `refund policy`) shows a warning: the brief
  marks each paraphrase pair as soft, and the matcher has no synonyms.
- **Load test:** restart the stack between runs (the rate limiter is in memory; `--fresh-db` also gives a clean
  usage total), then compare the script's
  totals with `GET /admin/usage` for the same key: requests, tokens and cost must match exactly.
- **Routing eval:** on the 20 provided cases the router scores 100% against 75% for a length-only rule that is
  given its best threshold (on the 8 trap cases: 8 of 8 against 5 of 8). Treat 100% as optimistic: the rules
  were written while the answer key was visible. On 40 further prompts written beforehand and not tuned to,
  the first run scored 82.5% (the length rule: 65%), and after two general fixes made because of that run,
  87.5%, which is no longer independent. The remaining misses are hard prompts with no keyword cue.
- **Cache eval:** on 26 paraphrase and 26 near-miss pairs written after the matcher and not tuned to it, the
  first run found 1 false hit at threshold 0.92 and 5 at 0.85. After two general fixes (negation, comparison
  direction) the results are: threshold 0.92, 11.5% of paraphrases hit, 0 false hits; threshold 0.85, 19.2%
  hit, 2 false hits (long prompts where one critical word differs). Recall is low because the matcher has no
  synonyms. A miss costs money and a false hit serves a wrong answer, so it errs towards misses.

**Reading latency numbers.** `latency_ms` in the request log is the time inside the gateway, and `upstream_ms`
is the part spent waiting on the provider; their difference is what the gateway adds. The provided load
test reports its own, larger figure, because it also counts connection setup and one-time client start-up on
each of its threads. On a healthy Linux setup it reported about 257 ms for requests the gateway timed at
20 to 75 ms. Quote the gateway's own numbers when judging the gateway.

## Known limitations

- **Single instance.** The rate limiter and the provider-health tracker are in memory, so they are per
  process and reset on restart. A multi-instance deployment needs a shared store such as Redis.
- **Budget overshoot** is bounded but real, as described above. `tokens_per_minute` is stored but not enforced.
- **Cache matcher:** low recall (no synonyms), English only, order-insensitive apart from the guarded words,
  and on a long prompt one differing word can still score about 0.89, a hit for a 0.85 key. A real embedding
  model behind the same two methods is the upgrade path. Concurrent identical requests all go upstream
  (no single-flight), though their entries are merged. Sampling parameters are part of the cache scope, but a
  high-temperature caller still gets the same cached text for the same prompt.
- **`auto` router:** keyword cues, so hard prompts phrased without any (some word problems, puzzles, ethics
  questions) are routed to the cheap tier. Only the last user message is judged.
- **Disconnected streams:** usage arrives only in the final chunk, so a client that hangs up early is logged
  with zero tokens and zero cost although the provider did some work.
- **Logging:** every rejection writes a log row, including unauthenticated ones, so a flood of bad requests
  grows the log. Sampling or aggregating `rejected_auth` is the mitigation.
- **Security scope:** one shared admin token, virtual keys in plain text, and cache scope per team (virtual
  key), not per end user. Good enough for a demo, not for production.

## Windows notes

- A **dead provider port** makes the connection attempt wait for a timeout instead of failing at once, so
  killing a provider process costs about a second per request until the circuit breaker trips. Setting the mock
  to `{"mode": "down"}` fails over instantly, and is the cleaner way to show failover.
- The sample config points the providers at `127.0.0.1` instead of `localhost`. On some Windows setups
  `localhost` is slower to connect to than `127.0.0.1`; this file differs from the course pack's original in
  those two lines only.
- In PowerShell, `curl` is an alias for `Invoke-WebRequest`. Use `curl.exe`, and send JSON from a file.
- `>` redirection in Windows PowerShell 5 writes UTF-16. Use `| Out-File -Encoding utf8` for files that Python
  must read.