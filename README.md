# Smart Dialer

A safe, concurrency-aware outbound dialing system for collections agents.

The whole project exists to answer one question:

> How do you get as much of the agent-utilization benefit of **predictive** dialing as possible,
> while keeping the deterministic safety characteristics of **progressive** dialing?

The answer this codebase implements: **prediction decides how many calls we would *like* to place;
a separate, deterministic Safety Controller decides how many calls may *actually* be placed.**
Prediction has no code path to the telecom provider at all — and there is a test that fails if
anyone ever adds one.

---

## Contents

- [The problem](#the-problem)
- [What is built](#what-is-built)
- [Architecture](#architecture)
- [Technology choices](#technology-choices)
- [Folder structure](#folder-structure)
- [State machines](#state-machines)
- [Concurrency: the hardest part](#concurrency-the-hardest-part)
- [The pacing formula, worked through](#the-pacing-formula-worked-through)
- [The Safety Controller](#the-safety-controller)
- [Mock providers](#mock-providers)
- [Failure handling](#failure-handling)
- [Database models](#database-models)
- [Running it locally](#running-it-locally)
- [Environment variables and secrets](#environment-variables-and-secrets)
- [Testing](#testing)
- [Simulation scenarios and measured results](#simulation-scenarios-and-measured-results)
- [Load testing and scaling](#load-testing-and-scaling)
- [Deployment](#deployment)
- [Assumptions and limitations](#assumptions-and-limitations)
- [What I would do with another week](#what-i-would-do-with-another-week)

---

## The problem

Collections agents spend a lot of their day waiting: waiting for a number to be dialed, waiting for
it to ring, waiting to find out nobody picked up. Software can do that waiting instead.

There are two classic approaches.

**Progressive dialing** is one available agent → one outbound call. If 50 agents are free, at most
50 agent-bound calls exist. It is completely predictable and completely safe, and it wastes time:
if only 20 % of calls are answered, agents spend most of their day listening to ring tone.

**Predictive dialing** starts calls *before* agents are free, betting on the answer rate. It uses
agents better, and when the bet is wrong it produces the thing nobody wants: a borrower answers and
there is no agent to talk to them.

This project keeps predictive's optimism and progressive's guarantees by putting a hard boundary
between them.

---

## What is built

- Working **Progressive Dialer** — the safe baseline
- Working **Predictive Pacing Engine** — explainable, deterministic, pure
- An **independent Safety Controller** with eight hard constraints
- **Call Allocator** that can only act on an approved `SafetyDecision`
- Explicit **agent** and **call** state machines with actor authorisation
- **Concurrency-safe** agent and borrower reservation using atomic conditional updates and leases
- **Idempotent** provider-event processing and out-of-order tolerance
- **Worker-crash recovery**, lease expiry, agent-disappearance handling, provider-outage response
- Two **mock telecom providers** — one well-behaved, one deliberately hostile
- **Provider health** tracking with a retry gate that prevents retry storms
- **Metrics** including agent utilization, and structured logs with phone-number redaction
- A **simulation harness** with four scenarios plus fault injection, driving the real system
- A **FastAPI** HTTP API and a **Streamlit** dashboard
- **Load tests** with measured numbers and an honest scalability analysis
- **725 automated tests**, including an executable version of the acceptance criteria

---

## Architecture

```
   Evaluator's browser
          │
          ▼
   Streamlit dashboard (dashboard/)          presentation only — no business logic, no database
          │  HTTPS + X-API-Key, server-side (no CORS involved)
          ▼
   FastAPI API layer (app/api/)              thin: validate, call a service, shape a response
          │
   ┌──────┴───────────────────────────────────────────────────────────┐
   │  Dialer worker loop (one tick per campaign)                      │
   │                                                                   │
   │   Campaign ──▶ Metrics snapshot ──▶ Pacing Engine                │
   │                                          │ PacingRequest          │
   │                                          ▼                        │
   │                                   Safety Controller               │
   │                                   (re-reads the database)         │
   │                                          │ SafetyDecision         │
   │                                          ▼                        │
   │                                    Call Allocator                 │
   │                            reserve agent + borrower, create call  │
   └──────────────────────────────────────────┼────────────────────────┘
                                              ▼
                                   TelecomProvider interface
                                       ├── Mock Provider A (clean)
                                       └── Mock Provider B (hostile)
                                              │ provider events
                                              ▼
                                      Event Processor
                                  idempotent · order-guarded
                                              │
   ┌──────────────────────────────────────────▼────────────────────────┐
   │              MongoDB — the single source of truth                 │
   └──────────────────────────────────────────▲────────────────────────┘
                                              │
                       Recovery worker (five sweeps) · Metrics sampler
```

Full component notes are in [docs/architecture.md](docs/architecture.md); the reasoning behind each
choice is in [docs/adr.md](docs/adr.md).

### The one rule that matters most

`app/pacing/` imports **no** provider module and **no** allocator module. `CallAllocator.allocate`
accepts a `SafetyDecision` and nothing else — there is no overload taking a plain integer. So there
is no code path from "the pacing engine wants 46 calls" to "46 calls were dialed" that does not go
through the Safety Controller.

`tests/test_architecture_boundaries.py` walks the import graph and asserts this. If someone adds a
shortcut later, the test fails.

---

## Technology choices

| Layer | Choice | Why | What it costs |
|---|---|---|---|
| Language | Python 3.11+ | One language for the whole project, so every line is defensible | Raw throughput; irrelevant for an I/O-bound dialer |
| API | FastAPI + Uvicorn | Async-native (matches the I/O-bound workload), Pydantic validation, automatic `/docs` | Slightly more ceremony than Flask |
| Database | MongoDB (Motor) | The concurrency primitive here is a **single-document atomic conditional update**, which Mongo gives us with no explicit transaction | Cross-document atomicity needs care — see below |
| Dashboard | Streamlit | No Node toolchain in a 4–6 hour prototype; the API key stays server-side; testable with Streamlit's own `AppTest` | Full script re-run per interaction; coarser layout control |
| Dashboard HTTP | `requests`, server-side | The Streamlit process calls the API itself, so **no CORS is involved** and secrets never reach a browser | One extra network hop |
| Refresh | `st.fragment(run_every="2s")` | Only live panels re-run, so controls stay responsive; no socket lifecycle code | ~2 s granularity, not a live stream |
| Testing | pytest, pytest-asyncio, a real test MongoDB | Concurrency tests need genuine `asyncio.gather` races against a real database | Needs a running MongoDB |
| Load testing | Custom asyncio harness | We need to stress *our* operations (reservation throughput), not HTTP endpoints — k6 would measure the wrong thing | Not an industry-standard tool |

### Deliberately not used

**Kafka / RabbitMQ** — one producer and a bounded worker set; MongoDB atomic claims already give
"exactly one worker gets this" with no extra service to deploy or explain.
**Redis** — nothing needs a shared sub-millisecond cache, and adding one would create the exact
"database says AVAILABLE, cache says RESERVED" ambiguity this design avoids by having no cache.
**Kubernetes / microservices** — one modular process plus background asyncio tasks. Module
boundaries give the separation; network boundaries would only add failure modes.
**An ML pacing model** — the formula has to be explainable in an interview.

---

## Folder structure

```
complete SB/
├── app/                          the backend (only this talks to MongoDB)
│   ├── main.py                   FastAPI app, lifespan, wiring of every component
│   ├── config.py                 all settings from environment variables + a startup invariant
│   ├── db.py                     Motor client singleton
│   ├── db_indexes.py             every index, including the unique ones that enforce idempotency
│   ├── logging_config.py         structured logging, noisy driver loggers pinned to WARNING
│   ├── models/                   Pydantic domain models and enums
│   ├── state_machines/           agent + call transition tables (pure, no I/O)
│   ├── repositories/             all database queries; atomic claims live here
│   ├── services/                 reservation, allocation, events, health, retry, availability
│   ├── safety/                   the Safety Controller, its constraints and fallback rules
│   ├── pacing/                   the predictive formula (imports no provider, no allocator)
│   ├── dialers/                  progressive / predictive / mode router
│   ├── providers/                TelecomProvider interface + the two mocks
│   ├── workers/                  dialer loop, recovery loop, worker identity
│   ├── metrics/                  utilization, campaign metrics, sampler, counters
│   ├── simulation/               scenario engine, fault injector, invariants, reports
│   ├── api/                      HTTP routes, schemas, error envelope, dependencies
│   └── utils/                    phone-number redaction
├── dashboard/                    the Streamlit app (imports nothing from app/)
│   ├── app.py                    entry point, tabs, auto-refresh
│   ├── api_client.py             the only contact with the backend
│   ├── formatting.py             display helpers
│   ├── views/                    one module per panel
│   └── requirements.txt          streamlit, requests, pandas — nothing else
├── tests/                        725 tests
├── loadtest/                     throughput and contention measurement
├── scripts/                      seed_data, seed_production, run_simulation
├── docs/                         architecture, ADR, state machines, scalability, deployment
├── requirements.txt              backend runtime dependencies
└── render.yaml                   backend deployment definition
```

---

## State machines

### Agents

```
OFFLINE → AVAILABLE → RESERVED → DIALING → CONNECTED → WRAP_UP → AVAILABLE
```

plus `PAUSED`, and a failure/release path back to `AVAILABLE` from `RESERVED`, `DIALING` or
`CONNECTED`, and `→ OFFLINE` from anywhere on logout or heartbeat timeout.

Every transition also records **who is allowed to trigger it** (`ALLOCATOR`, `EVENT_PROCESSOR`,
`RECOVERY`, `AGENT`, `WORKER_TIMER`). An event processor cannot reserve an agent; an allocator
cannot mark a call connected. Full table: [docs/agent-state-machine.md](docs/agent-state-machine.md).

### Calls

```
QUEUED → RESERVED → INITIATED → RINGING → ANSWERED → CONNECTED → COMPLETED
                              ↘ any non-terminal state → FAILED / CANCELLED
```

Each state has an integer **rank**. Terminal states (`COMPLETED`, `FAILED`, `CANCELLED`) are rank 6.
Three rules make bad carrier behaviour harmless:

1. **Terminal absorbs everything** — once terminal, later events change nothing.
2. **Rank never goes backwards** — a late `RINGING` after `CONNECTED` is ignored.
3. **Forward skips are allowed** — if `RINGING` is lost, `ANSWERED` still moves the call forward, so
   a missing event can never wedge a call.

Full table: [docs/call-state-machine.md](docs/call-state-machine.md).

---

## Concurrency: the hardest part

**The scenario:** Agent #10 is `AVAILABLE`. Worker 1 and Worker 2 both see it. Both try to reserve
it. Exactly one must win.

**The mechanism** — a single-document atomic conditional update:

```python
db.agents.find_one_and_update(
    filter={"_id": agent_id, "state": "AVAILABLE"},      # the condition
    update={"$set":  {"state": "RESERVED", "reserved_by": worker_id,
                      "lease_expires_at": now + ttl},
            "$inc":  {"state_version": 1}},
    return_document=AFTER,
)
```

MongoDB guarantees that a single-document update is atomic, so the read of `state: AVAILABLE` and
the write of `state: RESERVED` are one indivisible operation. There is no window between them.

The loser gets `None`. That is a **normal outcome**, not an error: it logs a contention counter,
skips that agent and tries the next candidate. It never retries the same document, which is how
contention storms start.

**This pattern is banned everywhere in the codebase:**

```python
agent = await db.agents.find_one({"state": "AVAILABLE"})   # ← window opens here
if agent:                                                  # ← another worker wins here
    await db.agents.update_one({"_id": agent["_id"]}, ...)  # ← both succeed
```

### Reservations are leases, never locks

Every reservation carries `reserved_by`, `reserved_at` and `lease_expires_at`. If a worker dies
holding one, the lease simply expires and the recovery worker reclaims it. **Nothing can be stuck
forever**, which is the property that makes crash recovery possible at all.

### The cross-document gap, stated honestly

Reserving an agent and reserving a borrower are two documents, so the pair is not atomic. Rather
than reach for a multi-document transaction, the allocator uses ordered **claim-then-compensate**:

1. Claim agent (atomic) — fail → nothing to undo.
2. Claim borrower (atomic) — fail → **release the agent**.
3. Create the call (unique `idempotency_key`) — duplicate → release both.
4. Dial — failure/timeout → fail the call, release both.

Every failure path has an explicit compensating release, **and** every reservation has a lease as a
second line of defence. If the process dies between steps 1 and 2, the lease expires and recovery
frees the agent. Safety never depends on the compensation actually running.

### Idempotency

- `calls.idempotency_key` is **unique** → a retried allocation cannot create a second call.
- `provider_events.(provider_name, provider_event_id)` is **unique** → the duplicate check happens
  *in the database*, atomically. Two workers processing the same webhook cannot both proceed; one
  gets a `DuplicateKeyError`. That is why the insert is the very first thing the processor does.

---

## The pacing formula, worked through

```
effective_answer_rate = clamp(0.7 × recent_rate + 0.3 × baseline_rate, 0.05, 0.95)

soon_free_agents = agents in WRAP_UP + agents CONNECTED longer than avg talk time
free_capacity    = AVAILABLE + soon_free_agents × SOON_FREE_WEIGHT
calls_needed     = free_capacity / effective_answer_rate        ← the core predictive idea
in_flight        = RINGING + INITIATED + RESERVED agents
raw_request      = floor(max(0, calls_needed − in_flight))
requested        = min(floor(raw_request × safety_margin × health_factor × volatility_factor),
                       MAX_REQUEST_PER_TICK)
```

**Worked example** (this is a real assertion in `tests/test_pacing_engine.py`):

> 12 agents free + 3 soon-free (weighted 1.5) = 13.5 capacity; at 32 % estimated answer rate that
> needs 42 calls; 21 already in flight leaves 21; ×0.85 safety margin ×1 health ×1 volatility =
> **17 requested**.

That sentence is generated by the code and stored with every decision, so "why 17 and not 10?" is
answered by fetching one document — and the dashboard shows it directly.

The blend with a baseline stops a handful of unlucky calls swinging pacing wildly. The clamp bounds
the damage when the estimate is simply wrong. `health_factor` is 1.0 / 0.5 / 0.0 for
HEALTHY / DEGRADED / UNHEALTHY, so a broken provider drives the request toward zero *before* the
Safety Controller even sees it.

**Progressive is the same formula** with `effective_answer_rate` forced to 1.0, `safety_margin` 1.0
and `soon_free_weight` 0.0 — which makes `calls_needed == free_capacity`. Progressive dialing is
the degenerate case of the predictive formula.

If the pacing engine throws, it returns `requested = 0`. A broken optimizer degrades to *no extra
calls*, never to unbounded ones.

---

## The Safety Controller

It takes a `PacingRequest` and returns a `SafetyDecision`. **It re-reads every number from the
database** rather than trusting the request — the request's numbers are recorded only for
comparison. `approved` is the minimum across eight hard constraints:

| # | Constraint | Limit |
|---|---|---|
| 1 | Agent capacity | `AVAILABLE_now − RESERVED_now` |
| 2 | Campaign concurrency | `max_concurrent_calls − active_calls` |
| 3 | Ringing ceiling | `MAX_RINGING_RATIO × available − ringing_now` |
| 4 | Provider health | HEALTHY → no cap; DEGRADED → progressive equivalent; UNHEALTHY → **0** |
| 5 | Progressive mode cap | In progressive mode, capped at `AVAILABLE_now` |
| 6 | Stale state | Snapshot too old, or unresolved expired leases → **0** |
| 7 | Availability drop | `AVAILABLE` fell more than the threshold → progressive fallback |
| 8 | Failure rate | Rolling **system** failure rate too high → progressive fallback |

Verdicts: `APPROVED`, `REDUCED`, `REJECTED`, `FALLBACK_PROGRESSIVE`. Every decision is persisted
with all eight constraint evaluations and the name of the binding one, so the dashboard can show
*which rule bit*.

Any exception during evaluation produces `approved = 0`. It fails closed, always.

**A subtle point worth defending:** approval is only an *upper bound*, not a guarantee. Between the
controller reading capacity and the allocator reserving agents, another worker may take some. That
is fine and is the layered design working: the Safety Controller sets the ceiling, and the atomic
reservation layer enforces the actual limit. This is exactly why the safety guarantee never depends
on the snapshot being perfectly fresh.

**Constraint 8 counts only *system* failures.** At a 20 % answer rate, 80 % of calls legitimately
end `FAILED` with reason `no_answer`. Counting those as failures would permanently trip the guard
and disable predictive mode entirely — which is precisely what happened until it was fixed. A
no-answer is a normal telephony outcome; a carrier rejection or timeout is a system fault.

---

## Mock providers

No real telecom account is needed. The mocks are the primary testing mechanism.

| | Mock Provider A | Mock Provider B |
|---|---|---|
| Setup latency | 150–250 ms | 800–2500 ms |
| Originate failures | ~2 % | ~15 % |
| Hangs past timeout | never | ~8 % |
| Duplicate events | never | ~10 % |
| Out-of-order events | never | ~10 % |
| Forced outage | — | `force_outage(seconds)` |

Provider B is the *test instrument* for the whole project: without a provider that genuinely emits
duplicates, shuffles events and times out, the idempotency and recovery work could not be honestly
verified. Both mocks use a seeded RNG, so any failing test is reproducible.

---

## Failure handling

| What goes wrong | What happens |
|---|---|
| Two workers race for one agent | One wins atomically; the loser counts contention and moves on |
| Worker crashes mid-allocation | Leases expire; recovery cancels the orphan call, releases both parties, and no duplicate call is created |
| Duplicate provider event | Unique index rejects the insert → `DUPLICATE_IGNORED`, zero side effects |
| Out-of-order event | Rank guard → `STALE_IGNORED`, call stays at the highest rank reached |
| Event for an unknown call | `INVALID_IGNORED`, logged, no crash |
| Provider rejects or times out | Call `FAILED`, both parties released, health records the failure |
| Provider outage | Health → UNHEALTHY → safety approves 0; **existing calls are never mass-cancelled**; retries suppressed by the retry gate |
| Agent stops heartbeating | Recovery takes them `OFFLINE`, cancels their call, releases the borrower |
| Agent stuck in WRAP_UP | Recovery returns them to `AVAILABLE` after a grace period |
| Retry exhaustion | Borrower → `EXHAUSTED`, never selected again |
| Safety evaluation throws | `approved = 0` — fail closed |
| Metrics collection throws | Logged and skipped; never allowed to affect dialing |

The recovery worker runs five bounded sweeps every `RECOVERY_TICK_SECONDS`. Each sweep is idempotent
and each write is conditional, so running several recovery workers at once is safe. If one sweep
throws, the others still run.

There is a config invariant asserted at startup:
`CALL_STALE_TIMEOUT_SECONDS > PROVIDER_TIMEOUT_SECONDS + RESERVATION_TTL_SECONDS`. Otherwise
recovery would cancel calls that are still being set up.

---

## Database models

MongoDB is the **single authoritative source of truth**. There is no cache, deliberately — so the
"database says AVAILABLE but the cache says RESERVED" question cannot arise.

| Collection | Holds | Notable indexes |
|---|---|---|
| `campaigns` | name, status, dialing mode, provider, caps, pacing config | — |
| `agents` | state, `state_version`, lease fields, heartbeat, time accounting | `(campaign_id, state)`, `(state, lease_expires_at)` |
| `borrowers` | phone number, status, lease fields, attempt count, backoff | `(campaign_id, status, next_eligible_at)` |
| `calls` | state, `state_rank`, `terminal`, provider ids, timestamps | **unique** `idempotency_key`; **unique** `(provider_name, provider_call_id)` |
| `provider_events` | every event with its `processing_status` | **unique** `(provider_name, provider_event_id)` |
| `pacing_decisions` | every requested number with all its inputs | `(campaign_id, created_at)` |
| `safety_decisions` | requested vs approved, all constraints, binding one | `(campaign_id, created_at)` |
| `metrics_samples` | periodic campaign snapshots | TTL index for retention |
| `provider_health_samples` | health history | `(provider_name, computed_at)` |
| `simulation_runs` | run history, status and final report | `started_at` desc, `status` |

Ignored events are stored too, with their reason. "We saw it and chose to ignore it" is far more
defensible than silence.

---

## Running it locally

You need **Python 3.11+** and a **MongoDB** instance (local `mongod` or a free Atlas cluster).

```bash
# 1. install
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt -r requirements-dev.txt

# 2. configure
copy .env.example .env            # then edit MONGODB_URI if it isn't localhost

# 3. seed some data
python -m scripts.seed_data --agents 10 --borrowers 300

# 4. run the API
uvicorn app.main:app --reload
```

The API is now on http://localhost:8000 and `/docs` shows every endpoint.

In a **second terminal**, run the dashboard:

```bash
.venv\Scripts\activate
set SD_API_BASE_URL=http://localhost:8000     # Windows
# export SD_API_BASE_URL=http://localhost:8000
streamlit run dashboard/app.py
```

The dashboard opens on http://localhost:8501. It needs the API running — it holds no data of its own.

To actually see calls flow: open the **Agents** tab and log agents in, then **Overview → Start**.
Set `DIALER_ENABLED=true` and `RECOVERY_ENABLED=true` in `.env` so the background loops run.

Run a simulation from the command line:

```bash
python -m scripts.run_simulation --scenario A --mode both --duration 600 --agents 10
```

Simulations seed and clear their own database (`<MONGODB_DB_NAME>_simulation`), so running one
never disturbs live campaign data.

Keep `--time-scale` modest. The scale compresses the simulated timeline but not real round trips
to the database or the provider, so `--duration 60 --time-scale 60` gives the run **one wall-clock
second** — less than a single dialer tick — and measures nothing useful.

---

## Environment variables and secrets

Backend (`.env`, see `.env.example` for the full list with safe defaults):

| Variable | Purpose |
|---|---|
| `MONGODB_URI` | **Required.** Connection string. No default — the app fails loudly without it |
| `MONGODB_DB_NAME` | Database name. Simulations never touch it — they run against `<name>_simulation` |
| `MONGO_MAX_POOL_SIZE` | Motor pool size; keep low on Atlas M0 |
| `API_KEY` | Shared secret for mutating endpoints; auth is off when empty |
| `CORS_ORIGINS` | For direct browser use of `/docs` only — *not* what makes the dashboard work |
| `DIALER_ENABLED` / `RECOVERY_ENABLED` | Start the background loops |
| `LOG_LEVEL` | `INFO` in production |
| `METRICS_RETENTION_MINUTES` / `DECISION_RETENTION_MINUTES` | TTL on sampled metrics and on pacing/safety decision records |
| Tuning | `RESERVATION_TTL_SECONDS`, `SAFETY_MARGIN`, `MIN/MAX_ANSWER_RATE`, `MAX_REQUEST_PER_TICK`, … |

Dashboard (`dashboard/.streamlit/secrets.toml`, see `secrets.toml.example`):

| Secret | Purpose |
|---|---|
| `SD_API_BASE_URL` | **Required.** Base URL of the API |
| `SD_API_KEY` | Same value as the API's `API_KEY` |

The dashboard **must not** be given `MONGODB_URI`. No real secret values appear anywhere in this
repository.

---

## Testing

```bash
pytest                    # the whole suite
pytest -k concurrency     # just the race tests
```

Tests need a reachable MongoDB; they use a separate `smartdialer_test` database and drop it between
modules. Load tests live in `loadtest/` and are excluded from the default run.

Highlights worth looking at:

| Test | What it proves |
|---|---|
| `test_acceptance_criteria.py` | **Start here** — one executable test per acceptance criterion |
| `test_agent_reservation_concurrency.py` | 20 workers race for 1 agent → exactly 1 wins |
| `test_progressive_safety.py` | Agent-bound calls ≤ N at 1, 5, 10, 50 agents and with 4 concurrent workers |
| `test_event_idempotency.py` | 5 duplicate events → 1 effect; 10 concurrent duplicates → 1 PROCESSED |
| `test_event_ordering.py` | `COMPLETED → ANSWERED → RINGING` leaves the call COMPLETED |
| `test_worker_crash_recovery.py` | Crash leaks nothing and creates no duplicate call |
| `test_provider_outage.py` | Outage stops new dialing without cancelling live calls |
| `test_safety_controller.py` | Every constraint has a test where it is the binding one |
| `test_pacing_engine.py` | The worked example above produces exactly 17 |
| `test_architecture_boundaries.py` | Pacing cannot reach the provider or the allocator |
| `test_dashboard_boundary.py` | The dashboard imports nothing from `app/` |
| `test_provider_b_soak.py` | 200 calls through the hostile provider, invariants hold |

---

## Simulation scenarios and measured results

The harness drives the **real** system — real agents in MongoDB, the real dialer, the real Safety
Controller. Only the *world* is simulated: who answers, how long they talk, when agents log off.

```bash
python -m scripts.run_simulation --scenario A --mode both --duration 900 --agents 10 --time-scale 60
```

| Scenario | Answer rate | Avg talk time |
|---|---|---|
| A | 20 % | 120 s |
| B | 50 % | 90 s |
| C | 70 % | 180 s |
| D | shifts 70 % → 30 % → 10 % | shifts 150 s → 210 s |
| faults | 30 % | 90 s, on hostile Provider B with an agent drop |

Every run asserts global invariants continuously: no agent bound to two active calls, no borrower in
two active calls, agent-bound calls ≤ usable agents, no call left non-terminal, no agent left
`RESERVED`.

### Measured result for Scenario A (20 % answer rate, 10 agents, 900 simulated seconds)

| Mode | Calls initiated | Completed | Utilization | Safety verdicts |
|---|---|---|---|---|
| Progressive | 41 | 9 | 18.8 % | 12 APPROVED, 2 fallback |
| Predictive | 41 | 9 | 18.9 % | **12 REDUCED**, 2 fallback |

**This result is the most interesting finding in the project, and it is not the one the plan
predicted.**

Predictive genuinely *asks* for far more (every tick produced a `REDUCED` verdict — it requested up
to 46 calls where progressive requested 5). But it **places the same number of calls**, because in
this architecture *every call reserves an agent before dialing*, and Safety Controller constraint #1
caps approvals at `AVAILABLE − RESERVED`. The optimizer is working, the boundary is working, and the
boundary wins — which is exactly the safety property the assignment asks for.

The honest conclusion: **in an agent-bound architecture, predictive dialing cannot beat progressive
on call volume.** Getting the real predictive win requires letting the dialer claim agents who are
in `WRAP_UP` or nearly finished — starting call setup *during* wrap-up so the call is already
ringing when the agent frees up. That is a genuine change to the claimable-state set, not a tuning
tweak, and it is the first item in [future work](#what-i-would-do-with-another-week).

I would rather report this than write a test that asserts a win the architecture cannot deliver.

---

## Load testing and scaling

```bash
python -m loadtest.run_all --scales 100 1000 10000
```

Measured on a local MongoDB 8.3, Motor pool size 100:

| Measurement | 100 agents | 1 000 agents | 10 000 agents |
|---|---|---|---|
| Agent reservation p95 | 54 ms | 427 ms | 4 053 ms |
| Call creation p95 | 20 ms | 185 ms | 4 409 ms |
| Event processing p95 | 120 ms | 1 470 ms | 39 579 ms |
| **Dialer tick p95** | **273 ms** | **438 ms** | **1 901 ms** |

**What breaks first: the dialer tick exceeds its 1 000 ms budget somewhere between 1,000 and 10,000
agents.** Once a tick takes longer than the interval the loop falls behind, the snapshot ages, and
the Safety Controller's staleness guard starts rejecting requests. The system stays *safe* — it
dials less — but stops being useful. That is the correct failure direction.

Across 44,000 concurrent reservation attempts at every scale, the load test asserted **zero
double-claims**. The concurrency guarantee holds under stress.

Full analysis, including why "add more servers" does not fix the first two bottlenecks:
[docs/scalability.md](docs/scalability.md).

---

## Deployment

Three free-tier pieces: **MongoDB Atlas M0** ← **Render** (FastAPI) ← **Streamlit Community Cloud**
(dashboard). Only the API process holds `MONGODB_URI`, which makes the dashboard's
presentation-only boundary a deployment fact rather than a convention.

`render.yaml` is committed. Step-by-step instructions, the environment-variable table, the
post-deploy smoke checklist and the free-tier caveats are in
[docs/deployment.md](docs/deployment.md).

**Public URLs:**

| Service | URL |
|---|---|
| Dashboard | https://smartdialer-dashboard.onrender.com |
| API | https://smartdialer-api-8di8.onrender.com |
| API docs | https://smartdialer-api-8di8.onrender.com/docs |

Both run on Render free instances, which sleep when idle — the first request after a quiet period
takes roughly a minute to wake the service.

---

## Assumptions and limitations

Stated plainly rather than hidden:

- **No real telecom integration.** Mock providers only, by design. The `TelecomProvider` interface
  is the seam where a Plivo or Twilio adapter would slot in.
- **Predictive does not beat progressive on volume here** — see the simulation section above. The
  architecture caps it, deliberately.
- **A single deployed instance runs one dialer worker.** The multi-worker concurrency guarantees are
  proven by tests and simulation, not by production traffic.
- **No cross-document transaction** between agent and borrower reservation. Leases are the
  mitigation and the exposure window is bounded by `RESERVATION_TTL_SECONDS`.
- **In-process metrics counters** (contention, retries) would undercount across multiple instances.
  The persisted `metrics_samples` are the cross-process source.
- **Utilization counts time in the current state as well as completed time**, so a live campaign
  reports a real number rather than lagging a whole call behind. It is still a point-in-time ratio,
  so a very short run reports a low figure simply because little time has accrued.
- **Free-tier hosts sleep**, and with two deployed processes there are two independent cold starts.
- **The dashboard is a ~2 s snapshot**, not a live stream. Events faster than a refresh cycle appear
  in the data but are not animated.
- **Streamlit suits an operations dashboard** for a few viewers. A production agent-facing UI would
  need a real web application.
- **Load-test numbers come from one machine** with client and server sharing CPU. The shape of the
  curve transfers; the absolute numbers do not.

---

## What I would do with another week

1. **Let the dialer claim soon-free agents** (`WRAP_UP`, nearly-finished `CONNECTED`) so predictive
   can start call setup before the agent is free. This is the change that would finally make
   predictive beat progressive, and it is a real state-machine change, not a tuning tweak.
2. **Per-campaign pacing loops and incremental counters** to fix the measured first bottleneck.
3. **Partitioned agent claiming** by `hash(agent_id) % worker_count` to cut contention.
4. **Partitioned event processing** by `hash(provider_call_id)`, preserving per-call ordering.
5. **A real provider adapter** behind the existing interface.
6. **Abandoned-call-rate tracking** as a ninth Safety Controller constraint — the regulatory one.
7. **Answer-rate modelling by time of day**, replacing the single rolling window.
8. **A push transport** (SSE or websockets) for sub-second dashboard updates.
