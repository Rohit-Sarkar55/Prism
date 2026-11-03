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
- [ ] **Phase 2: Core API, auth, headers (~1 day).** Non-streaming `/v1/chat/completions`,
  virtual-key auth, alias resolution, four headers, cost from price table.
- [ ] **Phase 3: Streaming (~1 day).** Chunk pass-through, `data: [DONE]`, usage recorded in
  a `finally` block (including client disconnect).
- [ ] **Phase 4: Allowlist, rate limits, budgets (~2 days).** Distinct errors, atomic
  enforcement. Done when a 50+ parallel request test shows zero over-admission and totals
  match the log. Riskiest phase: write tests first.
- [ ] **Phase 5: Routing, retries, failover (~1.5 days).** Fallback chain, backoff with
  jitter, no retry on permanent 4xx (except 429), fallback flag in header and log.
- [ ] **Phase 6: Smart routing `auto` (~2 days).** Difficulty scorer (not length-based),
  length-only baseline, one-command eval with per-case accuracy.
- [ ] **Phase 7: Semantic cache (~1.5 days).** Embeddings, cosine threshold, per-key (and
  per-model) scoping, tuned on paraphrase pairs, hit rate tracking.
- [ ] **Phase 8: Logs, usage API, console (~1.5 days).** Full request log, usage summary
  API, simple console. Numbers must match API and raw logs.
- [ ] **Phase 9: Verification and delivery (~2 days).** Unit tests, smoke, load test,
  routing eval, verification report, README, demo video.

## Current status

- Phase 0 DONE and pushed: setup, pack data in `data/ docs/ scripts/`, SQLite schema
  (`app/db.py`), nano-USD money helpers (`app/money.py`), seed loader (`app/seed.py`),
  settings (`app/config.py`).
- Phase 1 DONE (verify push): `app/providers/` has `base.py` (contract: `ProviderAdapter`,
  `ChatResult`, `StreamChunk`, `Usage`, `ProviderError`, `MidStreamError`),
  `openai_compat.py` (httpx adapter, timeouts, error classification, stream parsing),
  `fake.py` (scriptable in-process fake for tests), `registry.py` (builds adapters from
  config, `provider_for(model)`). `app/main.py` builds the registry at startup. 43 tests pass,
  including `tests/test_mock_integration.py` which runs the real `scripts/mock_provider.py`.
- `ruff.toml` excludes `scripts/` (provided files). Ruff 0.17 default rules are broad, so
  keep async tests free of blocking calls (use `httpx.AsyncClient` in async tests).
- Next: Phase 2, core API: `POST /v1/chat/completions` (non-streaming), virtual-key auth,
  alias resolution via the registry, the four `x-prism-*` headers, cost from the price
  table (integer nano-USD), request logging. Run on port 8080 and run `scripts/smoke_test.py`.

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