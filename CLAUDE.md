# Prism: LLM Gateway and Semantic Cache

Capstone project (Airtribe AI-first Software Engineering Program).
Repo: https://github.com/Rohit-Sarkar55/Prism

Prism is an OpenAI-compatible LLM gateway. One API sits in front of multiple model
providers and handles virtual keys, budgets, rate limits, failover, semantic caching,
cost metering and request logs. Two AI decisions run in the request path: difficulty
routing for `model: "auto"` and a semantic cache that matches meaning, not strings.

## How to work with me

- I am building this phase by phase (see Plan below). Work on the current phase only.
- Explain the "why" briefly when you make a design choice. This is a learning project and
  I must be able to explain every decision in the demo and README.
- Write tests for tricky logic before or together with the code, especially Phase 4.
- Keep `scripts/smoke_test.py` passing after every phase.
- Do not invent facts about the provided data files. Read the real files first.
- Commit-sized steps: finish one small piece, tell me how to verify it, then continue.

## Requirements from the brief (do not drift from these)

**Data to load and preserve:** `model_pricing.json`, `seed_keys.json`,
`gateway_config.sample.json`. Preserve seed key values and alias names (`fast`, `smart`,
`auto`) because the demo scripts reference them.

**API:** `POST /v1/chat/completions`, OpenAI-compatible, in front of at least 2 providers
(mocks count). Callers use a model alias or name. Provider credentials are never exposed.

**Header contract on every response:** `x-prism-provider`, `x-prism-cache`,
`x-prism-fallback`, `x-prism-cost-usd`.

**Streaming:** `"stream": true` passes tokens through as they arrive, no buffering. The
stream ends with `data: [DONE]`. Usage and cost are still recorded.

**Keys, limits, budgets:** authenticate every request with a virtual key. Enforce a
per-key model allowlist, requests-per-minute limit and monthly cost budget. Return a
distinct, documented error for each rejection, as an OpenAI-style body
`{"error": {"message", "type", "code"}}`. Mapping from docs/API_CONTRACT.md:
401 `authentication_error`, 403 `model_not_allowed`, 429 `rate_limit_exceeded`,
429 or 402 `budget_exceeded` (our choice, document it), 404 `not_found_error` (unknown
model/alias), 502 `upstream_error` (all providers failed).

**Routing and failover:** aliases resolve to a primary model plus an ordered fallback
chain. Retry transient errors with exponential backoff, then fail over and record it.

**Smart routing (`auto`):** classify prompt difficulty and route to `fast` or `smart`.
Must beat a length-only baseline. The eval set includes short-but-hard and long-but-trivial
traps. Provide a one-command routing eval that reports accuracy per case.

**Semantic cache:** serve cached responses for semantically similar prompts, scoped per
key (no cross-tenant leaks). Report hits via the header and track hit rate.

**Metering and logs:** cost = provider-reported token usage x price table. Log every
request (key, model, provider, status, tokens, cost, cache result, fallback flag, latency).
Expose a usage summary API.

**Ops console:** simple frontend showing usage by key, recent requests and cache hit rate.

**Technical requirements:**
- Persistent store for keys, usage, request logs and cache entries.
- Usage accounting and rate limiting must be correct under concurrent requests (no
  over-admission).
- Every upstream provider call has a timeout.
- Providers sit behind an adapter so mocks and real providers are interchangeable.
- Automated tests for cost computation, rate limiting, cache decisions, allowlists, routing.

**Deliverables:** working gateway and console, README (setup, API docs, architecture,
routing/failover design, cache scoping, design decisions, known limitations), public repo,
seeded demo keys with mock-provider instructions, verification report (smoke output, load
test, routing eval accuracy, over-admission results, accounting totals, latency summary),
and an explainer video (streaming, smart routing, budget rejection, rate limiting, cache
hit on a paraphrased prompt, live provider failover).

## Stack and decisions so far

- Python 3.11+, FastAPI, uvicorn, httpx, pydantic-settings, aiosqlite, numpy.
- SQLite in WAL mode as the persistent store (can move to Postgres later).
- Dev tools: pytest, pytest-asyncio, ruff, httpx2 (silences Starlette's TestClient
  deprecation warning; runtime provider calls still use `httpx`).
- httpx for provider calls (per-call timeouts, streaming without buffering).
- numpy cosine similarity for the semantic cache.
- Embedding approach is undecided. Leaning toward `fastembed` (small local ONNX model, no
  PyTorch). Decide in Phase 7. Fallbacks: sentence-transformers, or TF-IDF/hashed n-grams.
- Concurrency approach (decide and document in Phase 4): transactional check-and-increment
  for rate limits, reserve-then-settle for budgets.

Install:
```bash
python -m venv .venv
source .venv/bin/activate        # PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
python -m uvicorn app.main:app --reload
```

## Environment notes

- OS: Windows. Python 3.14.7. Terminals in use: PowerShell and Git Bash.
- The project root is the inner `Prism/` folder (it contains `app/`, `tests/`,
  `requirements.txt`). Run all project commands from there with the venv active.
- Use `python -m uvicorn ...` and `python -m pip ...` so the venv's tools are always used.
- Regenerate the lock file with `python -m pip freeze > requirements.lock.txt` in Git Bash,
  or `python -m pip freeze | Out-File -Encoding utf8 requirements.lock.txt` in PowerShell.
  Plain `>` in Windows PowerShell writes UTF-16 (file size roughly doubles). A correct
  file is about 670 bytes with the current dependencies.
- `pytest.ini` sets `asyncio_mode = auto`, so async tests need no decorator.
- In PowerShell, use `Invoke-RestMethod` instead of `curl`.
- Git pushes over HTTPS need a GitHub personal access token or browser sign-in (account
  passwords are rejected). Never commit tokens or write them in this file.

## Suggested layout

```
Prism/
  app/
    main.py          # FastAPI app
    config.py        # settings
    db.py            # sqlite setup and schema
    providers/       # adapter interface + mock providers
  data/              # model_pricing.json, seed_keys.json, gateway_config.sample.json
  scripts/           # smoke_test.py, load_test.py (provided)
  tests/
  requirements.txt
  requirements-dev.txt
```
Adjust to match what the repo already contains. Provided scripts and data may fix some paths.

## Plan (rough estimates for focused full-time work)

- [x] **Phase 0: Setup and data.** Done: venv, deps, pack data copied in, SQLite schema,
  seed loader, 10 tests passing, seeded DB verified (4 tenants). README skeleton still TODO.
- [x] **Phase 1: Provider adapter (~1 day).** (code written and tested, see status) The course pack PROVIDES the mock providers
  (`scripts/mock_provider.py`, stdlib only, ports 9001/9002, names alpha/beta, failure
  injection via `POST /admin/config` with mode ok/down/rate_limited, fail_rate, latency_ms).
  Do not rebuild them. Build the adapter interface (chat, stream) with httpx against them,
  a timeout on every call, and an in-process fake for unit tests.
- [x] **Phase 2: Core API, auth, headers (~1 day).** (non-streaming done, see status) Non-streaming `/v1/chat/completions`,
  virtual-key auth, alias resolution, four headers, cost from price table.
- [x] **Phase 3: Streaming (~1 day).** (code written and verified live, see status) Chunk pass-through, `data: [DONE]`, usage recorded in
  a `finally` block (including client disconnect).
- [x] **Phase 4: Allowlist, rate limits, budgets (~2 days).** (done, load test passes live) Distinct errors, atomic
  enforcement. Done when a 50+ parallel request test shows zero over-admission and totals
  match the log. Riskiest phase: write tests first.
- [x] **Phase 5: Routing, retries, failover (~1.5 days).** (done, drilled live) Fallback chain, backoff with
  jitter, no retry on permanent 4xx (except 429), fallback flag in header and log.
- [x] **Phase 6: Smart routing `auto` (~2 days).** (done, see status for honest numbers) Difficulty scorer (not length-based),
  length-only baseline, one-command eval with per-case accuracy.
- [ ] **Phase 7: Semantic cache (~1.5 days).** Embeddings, cosine threshold, per-key (and
  per-model) scoping, tuned on paraphrase pairs, hit rate tracking.
- [ ] **Phase 8: Logs, usage API, console (~1.5 days).** Full request log, usage summary
  API, simple console. Numbers must match API and raw logs.
- [ ] **Phase 9: Verification and delivery (~2 days).** Unit tests, smoke, load test,
  routing eval, verification report, README, demo video.

## Current status

- Phases 0 to 4 DONE and pushed. Phase 5 (retries, failover, circuit breaker; 146 tests) was
  delivered; confirm it is added and pushed. Provided load test passes on the user's machine
  (10 accepted of limit 10; usage API matches the client totals exactly: 120 prompt, 259
  completion tokens, 173400 nano-USD).
- Phase 6 DONE in code (verify tests + push): the `auto` router. New `app/difficulty.py`
  (`classify`), `route_auto` in `app/routing.py` now calls it, `tools/routing_eval.py` (the
  one-command eval: `python -m tools.routing_eval`, from the project root; needs `tools/__init__.py`),
  `data/routing_holdout.jsonl` (40 prompts the project author wrote BEFORE the classifier),
  `tests/test_difficulty.py`, `tests/test_auto_routing.py`. 186 tests pass in the sandbox.
- Verified live: easy prompts served by alpha-small (about 0.00001 USD), hard ones by alpha-large
  (about 0.0006 USD, 30 to 50x more), each with its `route_reason` in the request log.
- HONEST NUMBERS (put these in the README and verification report exactly as stated):
  * Provided set (20): router 20/20 = 100%; length-only baseline (given its best threshold, 46
    chars) 15/20 = 75%; on the 8 trap cases baseline 5/8, router 8/8. CAVEAT: the author could
    see this set's answer key while designing the rules, so 100% is optimistic.
  * Held-out set (40), the better estimate: FIRST RUN, nothing tuned: router 33/40 = 82.5%,
    baseline 26/40 = 65%. After two general fixes made because of what the first run exposed
    (vocabulary alone must not reach the threshold; "summarize" is only easy when there is text
    to summarize): 35/40 = 87.5%. That second number is NOT independent any more.
  * The 5 remaining misses are all hard prompts sent to the cheap tier, with no keyword cues
    (tricky word problem, logic puzzle, ethics argument, conceptual-depth question). They are
    documented, not patched with prompt-specific regexes.
- Not yet done: semantic cache (Phase 7), console (Phase 8), README, verification report, demo video.
- Smoke test still shows [5] and [6] failing until the cache exists.
- Next: Phase 7, the semantic cache. DECIDE and document: (a) embedding method (candidates:
  `fastembed` small ONNX model, no PyTorch, best fit for real paraphrases; or a no-dependency
  hashed word/char n-gram vector with numpy, weaker on true paraphrase); (b) similarity
  threshold per key from `virtual_keys.cache_threshold` (search 0.92, free-tier 0.85, others off);
  (c) scope entries by (virtual_key, model alias), NEVER across keys, with a test that proves a
  second key never gets the first key's answer; (d) cache hit: `x-prism-cache: hit`,
  `x-prism-cost-usd: 0`, log status `cache_hit` (counts in usage requests and cache_hits, cost 0),
  fallback false, provider header documented (e.g. `cache/hit` or the original provider);
  (e) streaming + cache: replay a hit as one SSE chunk plus [DONE], or skip the cache for streams,
  and document; (f) budget interplay: seed notes say budget-demo has the cache off so "a $0 cache
  hit cannot mask the rejection"; decide whether a hit is served when the budget is exhausted;
  (g) THE TRAP: smoke and load tests prepend a random salt like "(abc123)" to prompts; the load
  test uses cache-busting salts on the free-tier key which has the cache ON at 0.85, so make sure
  the matcher does not turn salted-but-different prompts into hits and break the load test's
  accounting; the paraphrase pairs (a1/a2, b1/b2) MUST hit and the near-miss a3 (password reset vs
  two-factor reset) MUST NOT. Only cache successful, non-streamed-error responses; never cache
  the response for a request that was rejected.

### Phase 6 design decisions (feed the README)

- Why rules, not a model: the router runs on every `auto` request, so it must be fast, free,
  deterministic and explainable (the reason string is logged per request). The mocks cannot
  classify anyway.
- Difficulty is a property of what the prompt ASKS FOR, not its length. Step 1 separates the
  instruction from payload: quoted spans and long or data-like sentences (pasted logs, rosters,
  emails) are ignored, so a long input with a trivial question stays trivial and jargon inside
  pasted data does not count. Step 2 scores the instruction with weighted signals.
- Hard signals (+): proofs/derivations (+3), reasoning requested, estimation, comparison,
  recommendation, causal "why"/mechanism, diagnosis, design/planning, algorithmic, non-trivial
  code, multi-part demands, a long multi-clause instruction, systems vocabulary (capped at +1.5 in
  total so jargon alone never decides). Easy signals (-): lookups, conversion/translation,
  extraction, light rewriting, summarising provided text, short-output requests, yes/no about a
  specific number.
- Decision: score >= 2.0 is `complex` (smart tier), else `simple` (fast tier). The bias is
  deliberate: below the threshold, pick the cheap tier.
- Only the LAST user message is judged; system prompts and earlier turns are ignored.
- A static test fails if the router source mentions the answer key or the eval files.
- Honesty process: held-out set written first; first-run numbers recorded; later tuning disclosed.
- Known limits: English only; keyword cues, so hard prompts without cues are missed (those errors
  cost quality, not money); no learning from outcomes. Next steps if time: a second-stage LLM
  judge on the fast model for scores near the threshold (cached), or a classifier trained on
  production logs with human labels.

### Phase 5 design decisions (feed the README)

- Chain: primary then fallbacks from the alias; a model maps to its provider via the registry.
- RETRY the same provider (exponential backoff 200 ms x2, +-25% jitter, max 3 attempts, from
  config `retry`) only for `server_error` (5xx) and `network` blips.
- FAIL OVER immediately, no retry, for `connect`, `timeout`, `rate_limited` (429) and
  `malformed_response`: retrying a dead, slow or rate-limiting endpoint only burns the caller's time.
- `client_error`: HTTP 400/413/422 means our request is bad, so STOP (another provider would
  fail the same way and cost quota); 401/403/404 are provider-side config problems, so fail over.
- One overall deadline (`PRISM_UPSTREAM_DEADLINE_S`, default 10 s) bounds retries plus failover;
  each attempt also has its own timeout (`PRISM_PROVIDER_TIMEOUT_S`, default 2 s; connect 1 s).
- `x-prism-fallback: true` only when a model other than the primary served it; the log row has
  `fallback`, `resolved_provider/model` (the one that SERVED it, and cost is priced as that model)
  and `retries` = total upstream attempts minus one, failovers included. A total failure is a
  generic 502 `upstream_error` (never provider details) and still records `retries`.
- Circuit breaker (the config's `degradation` block): a provider is degraded when it has 5
  consecutive failures (hard down) OR at least 10 recent calls with error rate >= 0.5 (window 60 s).
  Degraded providers are skipped unless they are the last model in the chain. One probe request
  is allowed per 5 s; a successful probe closes the circuit at once, so recovery takes seconds.
  The sample minimum stops a 30%-flaky provider tripping by chance (an earlier version with a
  4-sample minimum wrongly wrote off a flaky provider; the drill caught it).
  `p95_latency_ms` from the suggested config is NOT used: timeouts already count as failures.
- Health is per process, in memory (like the rate limiter).
- Streams use the same walk but only until the FIRST chunk (`open_stream`); after that, failure
  ends the stream with an error event (see Phase 3).
- Windows note: a dead port makes the connect attempt time out (about the 1 s connect timeout)
  instead of failing instantly, so killing a provider process costs ~1 s per request until
  the breaker trips (5 consecutive failures). `down` mode (503) is instant. For the demo video,
  use `{"mode": "down"}` for an instant failover, and mention this if killing the process.

### Phase 4 design decisions (feed the README)

- Check order: authenticate -> validate -> resolve model -> allowlist -> RATE LIMIT -> BUDGET ->
  (cache) -> upstream. Allowlist rejections do not consume rate-limit slots (a test pins this).
  A request rejected by the budget did consume a rate-limit slot.
- Rate limiter: sliding-window LOG per key (exactly at most `rpm` hits in any 60s window), in
  memory, race-safe because check-and-record is one synchronous step (no await between read and
  write) behind a lock. A threaded test fires 400 calls at a limit of 10 and requires exactly 10
  admitted. Rejected requests are not recorded, so hammering while limited does not extend the
  penalty. 429 carries `Retry-After`.
- State is per process and resets on restart (single-instance assumption; multi-instance would
  need Redis or similar). Memory is bounded by `rpm` timestamps per key.
- Budget: admit while `spent < budget`, add the real cost after the response. Spent is read from
  `usage_monthly` (one primary-key read, no log scan). Calendar month in UTC, keyed by `YYYY-MM`,
  so it resets by itself on the first request of a new month ("reset-on-read"), with no job.
- Documented overshoot: cost is only known after the provider answers, so a request admitted
  while budget remains may push spend past the budget, and requests in flight at the same time
  can each do so. Bound: roughly (requests admitted concurrently) x (cost of one request); the
  rpm limit also caps how many can be admitted per minute. Streams are admitted the same way.
- Budget exhaustion is HTTP 402 (not 429): SDKs auto-retry 429, and retrying cannot fix an
  exhausted budget. The rate limit is 429. Bodies are distinguishable by `type`.
- Not enforced: `tokens_per_minute` (stored in the DB, Good-to-Have in the brief).
- Known limitation: every rejection, including unauthenticated ones, writes a log row, so a flood
  of bad requests grows the log; mitigation would be sampling or aggregating `rejected_auth`.
- Cache hits and budget (Phase 7 decision pending): the seed notes the budget-demo key has the
  cache disabled "so a $0 cache hit cannot mask the rejection", which implies cache hits may be
  served even when the budget is exhausted. Decide and document.

### Phase 3 design decisions (feed the README)

- The gateway connects upstream and waits for the FIRST chunk before sending any HTTP response.
  A failure up to that point is a clean 502 JSON error (and can fail over in Phase 5). After the
  first chunk the client has output, so there is no failover.
- Provider dies mid-stream: HTTP status is already 200, so the stream ends with an
  `error` event (`code: stream_interrupted`) and NO `data: [DONE]` ([DONE] means success).
  The log status is `stream_error`. Never splice another provider's output.
- The gateway writes its own `data: [DONE]`; the adapter swallows the upstream one.
- `x-prism-cost-usd` is omitted on streams (headers go out before the cost is known; the API
  contract allows it). Cost comes from the final chunk's `usage`, goes to the log and usage
  API. The adapter requests `stream_options.include_usage` so real providers send it too.
- Every stream is recorded exactly once in a `finally`, with the cleanup under
  `anyio.CancelScope(shield=True)`: on client disconnect the request task is cancelled and an
  unshielded await would raise, losing the accounting. A test cancels a task group to prove it.
- Statuses that count in usage (SERVED): ok, cache_hit, stream_error, client_disconnect.
- KNOWN LIMITATION: usage arrives only in the final chunk, so a client that disconnects early
  is logged with 0 tokens and 0 cost although the provider did some work. A stream that ends
  cleanly without provider usage is recorded at zero cost with a server-side warning.
- Response sets `cache-control: no-cache` and `x-accel-buffering: no` (proxies must not buffer).
- Cache + streaming (Phase 7 decision pending): either replay a cached answer as a single SSE
  chunk plus [DONE], or skip the cache for streams. Document whichever is chosen.

### Phase 2 design decisions (feed the README)

- Check order: authenticate (401) -> validate body (400) -> resolve model (404) -> allowlist
  (403) -> [Phase 4: rate limit (429), then budget] -> [Phase 7: cache] -> upstream.
- 404 comes before 403 so a typo is reported as "unknown model", not "not allowed".
- Concrete model names (e.g. `alpha-small`) resolve too, but the allowlist is checked against
  the name as sent, so a key allowed only `fast` cannot call `alpha-small` directly.
- `usage.requests` counts SERVED requests (status ok or cache_hit); rejections and upstream
  failures are in the log but not in usage totals.
- Usage API aggregates from request_logs (arbitrary date windows, admin path). Budget checks
  will read `usage_monthly` (cheap, hot path). Both are written in one transaction, so they
  reconcile; a test asserts it.
- Cost is integer nano-USD end to end. Header `x-prism-cost-usd` prints 9 decimals ("0" when
  zero). The usage API returns `cost_usd` (float) and exact `cost_nano` (int).
- Only temperature, top_p, max_tokens, stop, seed, presence/frequency_penalty are forwarded
  upstream; other body fields are dropped.
- Error bodies never include upstream messages (they can describe our credentials); the
  detail goes to server logs only.
- Failure to log a REJECTION is swallowed (logged to the server log) so the caller still gets
  the real error; failure to record a SERVED request propagates as a 500 (never silently
  under-count).

### Phase 1 design decisions (feed the README)

- Adapter returns provider-reported usage; a 200 without usage is `malformed_response`
  because cost metering depends on it.
- `ProviderError.kind`: connect, network, timeout, rate_limited, server_error, client_error,
  malformed_response, stream_interrupted. `transient` = retrying the same provider may help
  (connect, network, timeout, 429, 5xx). Failover policy belongs to the gateway (Phase 5).
- `str(error)` is safe to show; `error.detail` (upstream message, key redacted) is for logs
  only and must never reach callers.
- Failure after the first streamed chunk raises `MidStreamError`: end the client stream with
  an error event, never fail over or splice another provider's output.
- Adapter yields parsed chunks and consumes upstream `[DONE]`; the gateway writes its own
  `data: [DONE]` so it controls termination. Streams request `stream_options.include_usage`.
- Timeout default 2.0s (`PRISM_PROVIDER_TIMEOUT_S`), low so the 3s slow-provider demo trips
  it. For streams, `read` timeout bounds the gap between chunks. Real providers need more.
- Model to provider rule: a provider serves models named `<provider>-...`, or lists them in an
  optional `models` array in its config entry.
- Provider API keys currently live in `gateway_config.sample.json` (mock keys only). Real keys
  must come from env or an uncommitted file. Document in README.

### Facts from the pack that shape the design

- Gateway must listen on port 8080 (the scripts' default URL); admin token is enough for
  admin routes.
- Seed keys: search (`prism-sk-search-1a2b3c`, allowlist fast, cache 0.92), research
  (`prism-sk-research-4d5e6f`, fast/smart/auto, cache off), free-tier
  (`prism-sk-free-7g8h9i`, 10 rpm, cache 0.85), budget-demo (`prism-sk-budget-demo-0j1k2l`,
  budget $0.00001, cache off). Preserve these values.
- Header `x-prism-provider` format is `alpha/alpha-small`. Cache hits cost `0`. On streaming,
  cost header is not required but final cost must still be logged and in the usage API.
- Every data-plane request is logged, including rejections: request_id, virtual_key,
  requested_model, resolved_provider/model, status, tokens, cost, cache, fallback,
  route_reason, retries, latency_ms, created_at.
- Cost precision: store cost as integer nano-USD (price per 1M tokens x 1000 = nano-USD per
  token, exact for the table's prices), print the header with enough decimals, and make the
  usage API sum the same stored values so the load test reconciles exactly.
- Budget rule (documented overshoot is allowed): admit while spent < budget, add actual
  cost after; in-flight requests may overshoot by their own cost. Reset: calendar month,
  reset-on-read.
- Rate limiter: in-memory is allowed but must be race-safe (no await between check and
  record, or a lock).
- Streaming: if a provider dies mid-stream, end the client stream with an error event; never
  splice another provider's output. Cache + streaming: replay as one SSE chunk or skip cache;
  document it.
- Router: `data/routing_eval.jsonl` has 20 cases; never use `expected_tier` in routing logic;
  length-only scores ~60%; 80%+ with a documented method is a solid baseline; log
  route_reason. Mocks cannot classify, so use heuristics, embeddings, or a local model.
- Cache: per key, matcher must hit the paraphrase pairs (a1/a2, b1/b2) and miss a3
  (password vs 2FA reset). Smoke and load tests prepend a random salt like `(abc123)` to
  prompts, so check how the matcher treats the salt and what that does to load-test hits.
- Verification: smoke test must pass; load test with `--rpm-limit 10` on the free-tier key
  must show zero over-admission; report routing accuracy, cache behavior on the sample pairs,
  added latency, and known limitations.

(Update this section at the end of each work session.)

## Known limitations (fill in as discovered; feeds the README)

- None recorded yet.