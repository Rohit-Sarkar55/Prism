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
distinct, documented error for each rejection: invalid key, model not allowed, rate
limited, budget exhausted. (Proposed status codes: 401, 403, 429, 402. Confirm against the
provided scripts and document them in the README.)

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
- Dev tools: pytest, pytest-asyncio, ruff.
- httpx for provider calls (per-call timeouts, streaming without buffering).
- numpy cosine similarity for the semantic cache.
- Embedding approach is undecided. Leaning toward `fastembed` (small local ONNX model, no
  PyTorch). Decide in Phase 7. Fallbacks: sentence-transformers, or TF-IDF/hashed n-grams.
- Concurrency approach (decide and document in Phase 4): transactional check-and-increment
  for rate limits, reserve-then-settle for budgets.

Install:
```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

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

- [ ] **Phase 0: Setup and data (~0.5 day).** Repo, venv, deps, README skeleton. Inspect the
  three data files, design tables (keys, usage, request logs, cache entries), write the
  loader. Done when: startup loads everything and seed keys are queryable.
- [ ] **Phase 1: Provider adapter and mocks (~1 day).** Adapter interface for chat and
  streaming. Two mocks that can inject latency, 429, 500 or go down. Timeout on every call.
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

- Phase 0 in progress.
- Done: repo created, `.venv` created, `requirements.txt`, `requirements-dev.txt` and
  `requirements.lock.txt` written.
- Next: add a root `.gitignore` (`.venv/`, `__pycache__/`, `*.db`, `*.db-wal`, `*.db-shm`,
  `.env`, `.pytest_cache/`, `.ruff_cache/`), add a minimal `/health` endpoint in
  `app/main.py` to verify setup, then inspect the three data files and design the schema.

(Update this section at the end of each work session.)

## Known limitations (fill in as discovered; feeds the README)

- None recorded yet.