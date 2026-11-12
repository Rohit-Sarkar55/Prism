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
- [x] **Phase 7: Semantic cache (~1.5 days).** (done, verified live) Embeddings, cosine threshold, per-key (and
  per-model) scoping, tuned on paraphrase pairs, hit rate tracking.
- [x] **Phase 8: Logs, usage API, console (~1.5 days).** (done, see status) Full request log, usage summary
  API, simple console. Numbers must match API and raw logs.
- [ ] **Phase 9: Verification and delivery (~2 days).** Unit tests, smoke, load test,
  routing eval, verification report, README, demo video.

## Current status

- Phases 0 to 5 delivered (Phase 5: retries, failover, circuit breaker). Phase 6 (`auto` router) delivered:
  the user's own run matches ours exactly (20/20 provided, 35/40 held-out, baselines 75% / 65%).
  Confirm Phases 5 and 6 are added and pushed.
- Phase 7 DONE in code (verify tests + push): the semantic cache. New `app/embedding.py` (the matcher),
  `app/cache.py` (scope, lookup, store, TTL, LRU), cache persistence in `app/store.py`, schema
  additions in `app/db.py` (with migrations), stream assembly and replay in `app/streaming.py`,
  cache step in `app/routes/chat.py`, `GET /admin/cache/stats` and `DELETE /admin/cache` in
  `app/routes/admin.py`. 256 tests pass in the sandbox. `tests/conftest.py` now has TWO clients:
  `client` (cache switched OFF for every key, for tests about other features) and `cache_client`
  (cache as seeded). Old tests keep using `client`.
- VERIFIED LIVE against the real mocks (put these in the verification report as stated):
  * Provided smoke test, search key (threshold 0.92) AND free-tier key (0.85): 17 passed, 0 failed,
    1 WARN (pair B). Run twice in a row on the persisted cache: "first request is a cache miss" still
    holds, because a salt is an identifier token and a different salt never matches.
  * Pair A (req_cache_a1 / a2): HIT, similarity 1.0. Near-miss a3: MISS, similarity 0.52.
    Pair B (b1 / b2): MISS, similarity 0.33 = the documented limitation (no shared content words;
    needs a real embedding model). Unrelated prompt: MISS (0.19).
  * Load test on a cache-ON key (free-tier): 10 accepted of limit 10, all 10 real upstream calls (cache
    stats: 0 hits, 10 misses: the random salts are identifiers, so they never match), and the usage API
    equals the client totals exactly. The exact token and cost totals differ from run to run because the
    mock's reply lengths vary (one verified run: 120 prompt, 276 completion tokens, 183600 nano-USD). USE
    THE USER'S OWN RUN for the verification report, never these numbers.
  * Tenant isolation live: same prompt on two cache-on keys: second key MISSES, then each hits only
    its own entry; a paraphrase of one key's prompt also misses on the other key.
  * Restart persistence: an entry stored before a gateway restart is hit (by a paraphrase) after it.
  * Latency: with the provider at 800 ms, miss 837 ms vs hit 21 ms. (With the mock's default 0 ms
    latency the difference is tiny; do not claim a speedup from that.)
  * Streaming: a cached answer is replayed as a proper SSE stream (31 chunks, [DONE]); time-sensitive
    prompt never cached.
- MATCHER EVAL (honest numbers; `python -m tools.cache_eval`, from the project root). The tool scores
  labeled pairs with the SAME matcher the gateway runs (`app/embedding.py`; a test fails if the tool ever
  imports a different one). Pairs: the 3 from the provided sample requests plus 49 in
  `data/cache_pairs_holdout.jsonl` (24 paraphrase, 25 near-miss, labels are the author's judgment). The
  pair file was written AFTER the matcher existed and without tuning to it, so the FIRST run is a fair
  held-out measurement:
  * FIRST RUN, nothing tuned: @0.92 paraphrases hit 3/26 = 11.5%, FALSE hits 1/26; @0.85 paraphrases hit
    5/26 = 19.2%, FALSE hits 5/26 = 19.2% (word-order swap n04; negation n17, n19; long prompts with one
    critical word swapped n08, n25).
  * Two general flaws found and fixed (so what follows is NOT independent any more): (1) negation
    ("with"/"without", not, cannot, n't) must agree between the two prompts, a hard block; (2) "than",
    "before", "after" keep their direction like to/from do, and direction detection now looks past
    function words ("before THE tests").
  * AFTER: @0.92: recall 3/26 = 11.5%, FALSE hits 0/26. @0.85: recall 5/26 = 19.2%, FALSE hits 2/26 = 7.7%
    (n08 and n25 only). Those two are a KNOWN, intentional limit: on a long prompt one differing word still
    scores ~0.89, a hit for a 0.85 key and a miss for a 0.92 key (a test pins this).
  * Recall is LOW and must be stated plainly: the matcher has no synonyms, so most real paraphrases
    ("price"/"cost", "money back"/"refund") miss. A miss costs money, a false hit serves a wrong answer,
    so the matcher errs towards misses. The brief accepts this when the provided pair A hits and the
    near-miss does not (both verified). Upgrade path: a real embedding model behind the same two methods.
- Phase 8 DONE in code (verify tests + push): the ops console. New `app/static/console.html` (ONE static
  page, vanilla JS, no build step, no external resources), `app/routes/console.py` (`GET /console`, loads
  without a token), `GET /admin/keys` (every key with limits, this month's spend, budget used %),
  `Store.keys_overview`. 265 tests pass in the sandbox. The page shows tiles (requests, spend, cache hit
  rate, saved by cache, provider health), usage by key with a budget bar (can exceed 100%: overshoot is
  documented), cache stats per key, provider health, and the 30 most recent requests (filterable by key,
  with status, provider, cache, fallback, retries, cost, latency and the routing / cache note). It
  auto-refreshes every 5 s; the admin token is typed in and kept in sessionStorage only (never in the
  page or a URL).
- SECURITY of the console: request logs hold text callers control (the model name they sent), so the page
  builds its DOM from text nodes only (a test forbids innerHTML, outerHTML, eval, document.write and
  friends), and the server sets a per-request nonce CSP (`default-src 'none'; script-src 'nonce-...';
  style-src 'nonce-...'; connect-src 'self'; frame-ancestors 'none'`), `Cache-Control: no-store` and
  `nosniff`. A bug fixed along the way: the admin token check used `secrets.compare_digest` on strings,
  which raises TypeError (HTTP 500) for any non-ASCII token; it now compares bytes, so a bad token is
  always a 401 (a test covers every admin endpoint).
- VERIFIED LIVE for the console: the real page JavaScript was run in jsdom against the live gateway with
  varied traffic (hits, misses, a fallback, a budget rejection, 429s, a bad key, and a request whose model
  name was `<img src=x onerror=alert(1)>`). Every table drew real data; the hostile name appeared as plain
  text with 0 <img> elements and 0 onerror attributes; a wrong token showed "The admin token was not
  accepted." and drew nothing; no token showed a prompt. NOT verified: it was never opened in a real
  browser (no browser in the sandbox), so layout, dark mode and CSP enforcement are untested. jsdom does
  not enforce CSP.
- README DONE: `README.md` at the project root (quick start, seeded keys, API with the full header and
  error tables, architecture, how each subsystem works and WHY, configuration, verification commands with
  the honest eval results, known limitations, Windows notes). `tests/test_readme.py` (12 tests) fails if the
  README drifts from the code: every route, `PRISM_*` setting, file path, seeded key, error status, default
  number and eval number it quotes is checked. It was mutation-tested (5 deliberate README errors, all
  caught). 277 tests pass in the sandbox. The README quotes NO smoke-test or load-test numbers: those belong
  in the verification report and must come from the user's own runs.
- DEV STACK DONE: `tools/dev_stack.py` (`python -m tools.dev_stack [--fresh-db]`) starts alpha, beta and the
  gateway, waits for each `/health`, logs to `.prism-logs/` (add that folder to `.gitignore`), refuses to start
  if a port is taken (a stale stack serves OLD code and config), and stops everything on Ctrl+C, SIGTERM or a
  closed terminal (signal handlers installed on purpose: a plain kill used to orphan the children, and a
  launcher started as a background job inherits "ignore SIGINT"). Verified for real: all three up, smoke
  test 17/17 through it, a second copy refuses, SIGINT and SIGTERM both leave no stray process, and the
  database persists across a restart. 284 tests pass in the sandbox. The user hit "mocks not running" three
  times (502s, 2.3 s bursts = two dead providers at ~1 s connect timeout each); this exists to end that.
- VERIFY DONE: `tools/verify.py` (`python -m tools.verify [--skip-tests] [--output PATH]`) starts a private
  stack on a TEMPORARY database (the user's prism.db is never touched) and runs: the pytest suite, the provided
  smoke test, a budget rejection, three failover drills (down, restored, 3 s slow), the load test with an exact
  reconciliation against `/admin/usage`, a latency summary from the request log (`latency_ms`, `upstream_ms`,
  their difference), cache tenant isolation plus persistence across a gateway restart, the routing eval and the
  cache eval; then writes `VERIFICATION.md` (summary table, raw output of each step, how to read the latency
  numbers, what the run does NOT show). `dev_stack` gained `start_stack`, `restart_gateway`, `ensure_ports_free`,
  `StackError` for this. 293 tests pass in the sandbox; a full real run passed all 9 graded checks (twice).
  The report MUST be generated on the USER'S machine (`python -m tools.verify`): one generated on Linux in the
  sandbox describes the sandbox and was deliberately not delivered.
- USER'S RESULTS SO FAR (Windows, real runs): smoke test 17 passed / 1 warning (pair B) / 0 failed; load test 10
  accepted of limit 10, 20 rate limited; gateway `latency_ms` 69-77 ms with `upstream_ms` 25-62 ms after the
  provider URLs became 127.0.0.1 (it was ~305 ms through `localhost`); the load-test script reported 313 ms
  average, the gap being client-side start-up (about 200 ms even on a healthy Linux setup). 272 tests passed
  there before test_readme.py was added (the user had not copied it yet). Launcher confirmed starting on
  Windows; Ctrl+C stopping all three on Windows was NOT yet confirmed.
- CONSOLE SEEN IN A REAL BROWSER (user's screenshot, Windows): layout, colors, pills and column alignment all
  fine, and every figure on screen cross-checked (tiles equal the table sums, 2 hits / 16 misses = 11.1%, 18
  requests served = 2 hits + 16 misses). One flaw found and fixed: a key that had spent $0.000174 of $5 showed
  "0% of $5.0000" with an empty bar (the API rounds to 0.1%); any nonzero spend now shows "<0.1%" and a sliver
  of bar (checked by rendering five budget cases in jsdom). Ctrl+C stopping all three services on Windows is
  now CONFIRMED (no listener left on 8080, 9001, 9002). Still pending from the user: the output of
  `python -m tools.verify` run on their machine.
- USER'S OWN VERIFICATION PASSED (`python -m tools.verify`, Windows, Python 3.14): all graded checks passed,
  293 tests, smoke 17/0 failed, budget, failover (down, restored, slow), no over-admission (10 of limit 10),
  accounting reconciled, cache isolation and persistence, routing and cache evals. Gateway latency from the
  log: 48 ms median total, 12 ms overhead. The user's `VERIFICATION.md` (their machine) is the deliverable.
- DEMO DONE: `tools/demo.py` (`python -m tools.demo [--no-pause] [--force]`): six narrated steps in the order of
  the brief's demo checklist (streaming, auto routing, semantic cache + tenant isolation, budget rejection,
  rate limit, live failover), pausing for Enter between steps, plain ASCII output. It refuses unless the stack
  runs on a CLEAN database (`dev_stack --fresh-db`) because the story needs an unspent budget and a free
  rate-limit window. Verified for real in the sandbox: full run, then a second run correctly refused. Two
  narration lines that overclaimed were found by reading the output and fixed (step 5 said "used 2" when it was
  1, now computed; it also claimed a concurrent burst while sending sequential requests).
- PYTHON 3.11 PROMISE: the README says 3.11+. The formatter rewrote two f-strings into a form valid only on 3.12+
  (a string inside an f-string field reusing the outer quote). Found with the tokenizer, fixed, and
  `tests/test_python_compat.py` now guards it (meaningful on 3.12+; the project was never run on 3.11).
  300 tests pass in the sandbox.
- REMAINING FOR THE USER: copy the new files, commit and push (including VERIFICATION.md), then record the
  explainer video (use the demo command and the console). Submission checklist: public repo link, README,
  seeded keys + reproduce instructions (README section "Reproduce the demo"), VERIFICATION.md, video.
- SANDBOX LESSONS (for whoever drives the sandbox): never `kill` from stale PID files (PIDs get reused, one
  killed the tool's own shell), never `pkill -f` a pattern that appears in the same command line, find a
  process by its exact command instead.
- OPEN: the latency question (the load test shows ~730-940 ms average on the user's Windows machine; ~257 ms
  on the sandbox). What has been established so far:
  * The gateway adds little: gateway overhead (`latency_ms - upstream_ms`) was 6-28 ms under 10 concurrent
    requests on the user's machine. Quote the gateway's own `latency_ms` from the request log, not the
    script's figure.
  * The provided load test's own latency figure includes ~200 ms of client-side cost even on a healthy
    Linux setup (script 257 ms vs gateway 20-75 ms for the same requests; likely one-time `urllib` start-up
    on each thread's first request). Not yet confirmed on Windows: the user has NOT yet run the `urllib`
    cold-vs-warm probe.
  * The user's table showed `upstream_ms` ~305 ms for an INSTANT mock (healthy baseline here: 9-53 ms). It
    came from a run that may have used the OLD `localhost` provider URLs, because the gateway may not have
    been restarted after the config change. A later paste of the same query returned the IDENTICAL ten rows
    (same timestamps, 16:31:33), so it still does not show a post-restart run. A clean re-measure needs:
    restart gateway, note a timestamp, run the load test, then list only rows newer than that timestamp.
  * Wrong explanations already ruled out (do not repeat them): "the load-test client's localhost is slow
    because of IPv6" (Python resolves `localhost` to [v4, v6] and connects in 2-21 ms); "curl's 215 ms for
    localhost" is a curl effect (it prefers IPv6, waits 200 ms), not Python's.
  * The sample config now points both providers at 127.0.0.1 (a precaution; it did not change the client
    figure by much).
- Not yet done: the verification report (run `python -m tools.verify` on the user's machine, then commit
  smoke output, load test, over-admission, accounting totals vs the usage API, routing eval, cache eval,
  latency from the log), and the demo script. The demo script is best built as `tools/demo.py`: one command
  that narrates streaming, auto routing, a cache miss then hit then paraphrase hit then near-miss, tenant
  isolation, the rate limit, budget rejection, and failover (set alpha down via POST /admin/config on the
  mock, then restore). Do not add it to the README until it exists (the README test checks file names).

### Phase 7 design decisions (feed the README)

- MATCHER: a documented local substitute, `lexical-content-words-v1` in `app/embedding.py`, NOT a
  neural embedding model. Chosen because the sandbox could not download model weights, so a real
  model could not be tested, and the brief allows "a clearly documented local substitute" provided the
  paraphrase pairs hit. The `Embedder` shape (`embed`, `similarity`) is the seam for swapping in a real
  model (a Stretch item in the brief); nothing else knows how the score is produced. If the user wants
  to try it on their machine (needs internet for the model, and a Python version onnxruntime supports,
  which is uncertain for 3.14), add an embedder with the same two methods and switch it in `main.py`.
- Similarity = cosine of binary stemmed content-word vectors, times 0.8 if the question type differs
  (how / why / what / when / where / who / which / yes-no / other). Stopwords and frame words ("how do I",
  "what are the steps to") are removed; negations are kept as content.
- Guards that force a score of 0: identifier-like tokens (anything containing a digit: order numbers,
  versions, the test scripts' random salts) must match EXACTLY; negation must agree ("with" vs
  "without", not, cannot, n't); directed pairs around to/from/into/onto/vs/than/before/after must match
  when both prompts have them ("convert USD to EUR" is not "convert EUR to USD", "A faster than B" is
  not "B faster than A"); the nearest content word on each side counts, skipping function words.
- Seed thresholds are used as given (search 0.92, free-tier 0.85; research and budget-demo cache off).
  With this matcher 0.92 means near-identical content, 0.85 tolerates about one differing word in 7.
- SCOPE (exact-match filters, never similarity): virtual key, model/alias as sent (`fast`, `smart`,
  `auto` are separate), parameter fingerprint (temperature, max_tokens, ...), and the system prompt +
  earlier turns (exact). Similarity is applied only to the LAST user message. Multi-turn choice:
  last user message by similarity within an exactly-matching conversation prefix.
- NEVER cached or looked up: key with cache off; time-sensitive prompts (current, now, today, latest,
  status of, weather, ...); prompts over 4000 chars; errors; partial streams; responses with no text.
- Cache hit: `x-prism-cache: hit`, `x-prism-provider: cache`, `x-prism-cost-usd: 0`, `x-prism-fallback:
  false`; log status `cache_hit`, `cache` = hit, `resolved_provider/model` null, `cache_similarity` set,
  `route_reason` notes the hit. Response body `usage` is ZERO and the log tokens are zero: usage tokens
  mean "tokens consumed upstream", so tokens x price = cost for every request.
- Log `cache` values: `hit`, `miss` (cache consulted, no match), `bypass` (not consulted: key off,
  uncacheable prompt, or rejected before the cache). The header is `miss` for both miss and bypass
  (the contract only allows hit or miss). Hit rate = hits / (hits + misses); bypass is excluded.
- Order: budget is checked BEFORE the cache, so a key with an exhausted budget is rejected even for a
  free cached answer (strict, documented). Hits still consume rate-limit slots.
- Streaming: a completed stream is assembled and cached; a hit is replayed to a streaming caller as a
  proper SSE stream (role chunk, word chunks, final chunk, [DONE]); streams and non-streams share entries.
- Lifecycle: TTL 24 h (`PRISM_CACHE_TTL_S`), at most 500 entries per key (`PRISM_CACHE_MAX_ENTRIES_PER_KEY`),
  least recently used evicted first, expired entries purged on the next write. Persisted in SQLite, with a
  lazy per-scope in-memory index. Concurrent identical requests both go upstream (no single-flight) but
  their entries are merged, never duplicated.
- The cache is an optimisation: lookup/store errors are logged and treated as a miss / skipped.
- Privacy: an entry holds the full response and the first 200 chars of the prompt; the request log
  holds no bodies. Scope is per virtual key (team), not per end user.
- Known limitations: low recall (see MATCHER EVAL); word-based (pair B misses; also misses paraphrases with different vocabulary);
  English only; a one-word difference below the threshold can still hit on long prompts; per-process
  in-memory index (single instance); no single-flight; sampling parameters are in the scope key, but
  high-temperature callers still get the same cached text for the same prompt.

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