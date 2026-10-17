# Transaction-Webhook-Service

Webhook service that accepts payment transactions instantly (202) and processes them reliably in the background, with idempotency and crash recovery. FastAPI + PostgreSQL.

**Live API (base URL):** https://transaction-webhook-service-qgm4.onrender.com

| Endpoint | Live URL |
|---|---|
| Health check | `GET https://transaction-webhook-service-qgm4.onrender.com/` |
| Transaction webhook | `POST https://transaction-webhook-service-qgm4.onrender.com/v1/webhooks/transactions` |
| Transaction status | `GET https://transaction-webhook-service-qgm4.onrender.com/v1/transactions/{transaction_id}` |
| Readiness (checks the database) | `GET https://transaction-webhook-service-qgm4.onrender.com/ready` |
| **Interactive docs**: try every endpoint in the browser | https://transaction-webhook-service-qgm4.onrender.com/docs |

---

## Contents

- [For reviewers](#for-reviewers)
- [How it works](#how-it-works)
- [API](#api)
- [Running locally](#running-locally)
- [Deployment](#deployment)
- [Testing](#testing)
- [Local vs live performance](#local-vs-live-performance)
- [Technical choices](#technical-choices)
- [Configuration](#configuration)
- [Decisions on the spec](#decisions-on-the-spec)
- [Known limitations and next steps](#known-limitations-and-next-steps)

---

## For reviewers

Everything below runs against the live service. The database starts empty.

> **Use a new `transaction_id` for each fresh test**, for example `txn_review_01`, `txn_review_02`.
> An ID that has already been sent is (correctly) treated as a duplicate, so the brief's example ID
> only behaves like a new transaction the first time anyone sends it.

### Option 1: in the browser (no tools needed)

1. Open **https://transaction-webhook-service-qgm4.onrender.com/docs**.
2. Expand **`POST /v1/webhooks/transactions`** and click **Try it out**. The body is pre-filled
   with the brief's example; change `transaction_id` to a new value, then click **Execute**.
   Expect **`202`** with `{"status": "ACCEPTED", ..., "duplicate": false}`.
3. Click **Execute** again with the same body. Expect **`202`** again, now with
   `"duplicate": true`: nothing new is stored or processed.
4. Expand **`GET /v1/transactions/{transaction_id}`**, click **Try it out**, enter the same ID,
   and click **Execute**. Expect a one-item list with `"status": "PROCESSING"` and
   `"processed_at": null`.
5. Wait about 30 seconds and execute it again. Expect `"status": "PROCESSED"`, with `processed_at`
   about 30 s after `created_at`.
6. Expand **`GET /`** and execute it. Expect `{"status": "HEALTHY", "current_time": "...Z"}`.

### Option 2: command line (curl)

```bash
BASE=https://transaction-webhook-service-qgm4.onrender.com
ID=txn_review_$(date +%s)          # a fresh ID every run

# 1. Webhook: 202 straight away (time_total is the full round trip from your machine)
curl -s -w "  -> HTTP %{http_code} in %{time_total}s\n" -X POST $BASE/v1/webhooks/transactions \
  -H "Content-Type: application/json" \
  -d "{\"transaction_id\":\"$ID\",\"source_account\":\"acc_user_789\",\"destination_account\":\"acc_merchant_456\",\"amount\":1500,\"currency\":\"INR\"}"

# 2. Same webhook again: 202, "duplicate": true
curl -s -X POST $BASE/v1/webhooks/transactions -H "Content-Type: application/json" \
  -d "{\"transaction_id\":\"$ID\",\"source_account\":\"acc_user_789\",\"destination_account\":\"acc_merchant_456\",\"amount\":1500,\"currency\":\"INR\"}"; echo

# 3. Status now: PROCESSING, processed_at null
curl -s $BASE/v1/transactions/$ID; echo

# 4. Status after the 30-second processing delay: PROCESSED
sleep 32; curl -s $BASE/v1/transactions/$ID; echo

# 5. Health check
curl -s $BASE/; echo
```

### Option 3: one command that checks everything

```bash
python scripts/smoke_test.py https://transaction-webhook-service-qgm4.onrender.com     # standard library only, ~40 s
```

It sends a webhook and checks the 202 and the 500 ms limit, a repeat, 10 simultaneous duplicates,
the `PROCESSING` → `PROCESSED` transition after the delay, plus `404` and `422` handling. It prints
PASS/FAIL per check.

### How each part of the brief is met

| Brief | Requirement | How to check (live) | Expected |
|---|---|---|---|
| 1. API | `POST /v1/webhooks/transactions` with the given body | Option 1 step 2 / Option 2 step 1 | `202 Accepted` |
| 1. API | `GET /` health check | Option 1 step 6 / Option 2 step 5 | `{"status":"HEALTHY","current_time":"<UTC>Z"}` |
| 1. API | `GET /v1/transactions/{id}` | Option 1 steps 4–5 / Option 2 steps 3–4 | one-item list with all fields from the brief |
| 2. Response | `202` within 500 ms, simple acknowledgement | `time_total` in Option 2 step 1; `Server-Timing` response header | ~5–20 ms inside the app (`Server-Timing: app;dur=…`); total = that + network to Singapore |
| 3. Background | processed after a 30 s delay, stored persistently | Option 2 steps 3–4 | `PROCESSING` → `PROCESSED`; `processed_at` ≈ `created_at` + 30 s; stored in Postgres |
| 4. Idempotency | same `transaction_id` → one processed transaction, no errors | Option 1 step 3 / Option 2 step 2 / smoke test | `202` with `"duplicate": true`; still one transaction |
| 5. Storage | status and timing stored | Option 2 step 4 | `status`, `created_at`, `processed_at` |
| Success 1 | single transaction processed after ~30 s | Option 2 steps 1–4 | `PROCESSED` ~30 s later |
| Success 2 | duplicates processed once | smoke test (10 simultaneous duplicates) | exactly one `"duplicate": false` |
| Success 3 | fast under processing load | send webhooks while others are processing; `scripts/burst_test.py`, `scripts/loadtest.py` | still `202` with ~5 ms in the app ([live results](#local-vs-live-performance)) |
| Success 4 | errors handled, nothing lost | bad body → `422`, unknown ID → `404`; database unreachable → `503` (not stored, so the sender retries); crash recovery and retries are covered by the [automated tests](#automated-tests) | no `500`s; every `202` is stored and processed |

**Notes on timing.** The service runs in Singapore, so total response time includes the network
from wherever you are (~60–150 ms per round trip from India during testing). The `Server-Timing`
header on every response shows the time spent inside the app alone. The free Render instance is
kept awake by an uptime ping, so there is no cold start.

---

## How it works

```
 Payment provider ──POST /v1/webhooks/transactions──▶ API
                                                     1. validate the body (422 if invalid)
                                                     2. INSERT ... ON CONFLICT (transaction_id) DO NOTHING
                                                     3. reply 202, typically in ~10 ms

 Background worker (runs inside the same app, started at startup)
                                                     a. claim up to 100 rows: FOR UPDATE SKIP LOCKED + a time-limited lease
                                                     b. simulated external call (30 s)
                                                     c. mark PROCESSED, set processed_at
                                                     d. on error: retry with backoff; after 5 attempts, mark FAILED

 Postgres (Neon) holds the transactions and doubles as the job queue.
```

The webhook handler does almost nothing on purpose: it stores the transaction and replies. The
slow work happens in the worker. Because the row is committed **before** the 202 is returned,
an accepted transaction can't be lost: if the process crashes mid-job, the lease runs out and the
job is picked up again.

---

## API

### `POST /v1/webhooks/transactions`

```json
{
  "transaction_id": "txn_abc123def456",
  "source_account": "acc_user_789",
  "destination_account": "acc_merchant_456",
  "amount": 1500,
  "currency": "INR"
}
```

| Response | When |
|---|---|
| `202 {"status":"ACCEPTED","transaction_id":"...","duplicate":false}` | new transaction stored and queued |
| `202 {..., "duplicate":true}` | this `transaction_id` was already received; nothing new is processed |
| `422` | invalid body: missing field, amount ≤ 0 or more than 2 decimals, currency not a 3-letter code |
| `429` + `Retry-After` | the client exceeded the rate limit |
| `503` | the database was unreachable, so the webhook was **not** stored; the sender should retry |

### `GET /v1/transactions/{transaction_id}`

Returns a one-item list, as in the spec. `404` if the ID is unknown.

```json
[{
  "transaction_id": "txn_abc123def456",
  "source_account": "acc_user_789",
  "destination_account": "acc_merchant_456",
  "amount": 1500.0,
  "currency": "INR",
  "status": "PROCESSED",
  "created_at": "2026-10-17T10:30:00Z",
  "processed_at": "2026-10-17T10:30:30Z"
}]
```

`status` is `PROCESSING`, `PROCESSED`, or `FAILED` (see [Decisions on the spec](#decisions-on-the-spec)).

### `GET /`

```json
{"status": "HEALTHY", "current_time": "2026-10-17T10:30:00Z"}
```

A **liveness** check: it doesn't touch the database, so a database problem never makes the
platform restart a healthy process.

### `GET /ready`

```json
{"status": "READY", "current_time": "2026-10-17T10:30:00Z"}
```

A **readiness** check: it runs `SELECT 1` and returns `503 {"status": "UNAVAILABLE"}` if the
database can't be reached. It is not rate limited.

---

## Running locally

Requires Python 3.12 and a PostgreSQL database (a free [Neon](https://neon.tech) project works,
or `docker run -e POSTGRES_PASSWORD=dev -p 5432:5432 postgres:16`).

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

cp .env.example .env          # then set DATABASE_URL
uvicorn app.main:app --reload
```

The `transactions` table is created automatically on startup.

With Docker:

```bash
docker build -t transaction-webhook-service .
docker run --env-file .env -p 8000:8000 transaction-webhook-service
```

---

## Deployment

Deployed as a Docker web service on **Render**, with **Neon** Postgres, both in **Singapore**, so
each database round trip takes a few milliseconds.

1. **Database (Neon):** create a project in AWS Asia Pacific (Singapore). Copy the *direct*
   connection string (connection pooling off). The app removes `sslmode`/`channel_binding` from
   the URL itself and applies SSL through the driver.
2. **Service (Render):** New → Web Service → this repository. Settings:
   - Language **Docker**, branch `main`, region **Singapore**
   - Environment variable `DATABASE_URL` = the Neon string (everything else has defaults; see
     [Configuration](#configuration))
   - Health check path `/`, auto-deploy on commit
3. **Keep-alive:** an uptime job on [cron-job.org](https://cron-job.org) calls `GET /ready` every 4
   minutes, which keeps both the free Render instance and the scale-to-zero Neon database awake
   (see [Known limitations](#known-limitations-and-next-steps)).
4. **Verify:** `python scripts/smoke_test.py <live-url>`

On shutdown (every deploy), the app releases the leases of transactions it was still processing,
so the new instance picks them up immediately instead of waiting for the leases to expire.

---

## Testing

### Automated tests

```bash
# Needs a separate, disposable database; the suite refuses to run against DATABASE_URL.
# Set TEST_DATABASE_URL in .env, then:
pytest -v
```

The tests use a 2-second processing delay instead of 30 seconds so the suite runs quickly. Every
test starts with an empty table.

**30 tests**, grouped by what they prove:

| Area | What is tested |
|---|---|
| Intake | a valid webhook returns `202`, is stored as `PROCESSING`, and the amount is stored exactly (`Decimal`) |
| Fast acknowledgement | the response comes back while the transaction is still `PROCESSING`; the request never waits for the work |
| Idempotency, sequential | the same webhook sent 3 times gives `duplicate` = `false, true, true` and exactly one row |
| Idempotency, concurrent | **10 identical webhooks sent at the same moment** give exactly one stored transaction and one `duplicate:false` |
| Validation | negative or zero amount, more than 2 decimals, non-numeric amount, bad currency, empty ID, null or missing fields: all `422`, nothing stored |
| Normalisation | a currency like `" usd "` is stored as `USD` |
| Lookup | unknown ID gives `404`; known ID returns the spec's list shape with UTC `Z` timestamps and `processed_at: null` while processing |
| Processing | a transaction reaches `PROCESSED` no sooner than the configured delay, on the first attempt, with its lease cleared |
| Crash recovery | a row whose worker "died" (expired lease) is picked up and completed |
| Lease respected | a row currently leased by another worker is **not** taken |
| Retry | the first attempt fails, the retry succeeds, and the error is cleared |
| Give up | after the maximum number of attempts, the transaction is `FAILED` with the error recorded |
| Rate limiting | burst allowed then refused; tokens refill over time; separate buckets per client; idle clients are forgotten; API returns `429` with `Retry-After`; health check is never limited |
| Health and readiness | `GET /` returns `HEALTHY` with a UTC timestamp; `GET /ready` returns `READY` when the database answers and `503 UNAVAILABLE` when it doesn't; every response carries a `Server-Timing` header |

### Smoke test against a running deployment

```bash
python scripts/smoke_test.py <live-url>          # standard library only, nothing to install
```

It checks, in order: `GET /` and `GET /ready`; a webhook answered with `202` within 500 ms; a
repeat flagged as a duplicate; 10 simultaneous duplicates giving exactly one new transaction; the
lookup showing `PROCESSING`; `404` for an unknown ID; `422` for an invalid amount; and, after
waiting 35 s, the transaction `PROCESSED`. It exits non-zero if any check fails.

### Manual test against the live service

Use the steps in [For reviewers](#for-reviewers). To see duplicate protection under concurrency:

```bash
for i in $(seq 1 10); do
  curl -s -X POST $BASE/v1/webhooks/transactions -H "Content-Type: application/json" \
    -d '{"transaction_id":"txn_dup_check","source_account":"a","destination_account":"b","amount":10,"currency":"INR"}' &
done; wait
curl $BASE/v1/transactions/txn_dup_check    # one transaction
```

### Load testing

```bash
# Unique webhooks, Python client (reports throughput + p50/p95/p99 latency)
python scripts/loadtest.py http://localhost:8000 3000 20

# Server ceiling with ApacheBench (same body repeated, so it exercises the duplicate path)
ab -k -n 5000 -c 100 -p body.json -T application/json http://localhost:8000/v1/webhooks/transactions
```

Results on a laptop (single uvicorn process, local Postgres, rate limiting off for the
measurement). Every request succeeded in every run. These were measured before the round-trip
optimisations described under the burst test, so they are conservative.

| Endpoint | Concurrency | Throughput | p50 | p95 | p99 | Max |
|---|---|---|---|---|---|---|
| `GET /` | 20 | ~7,790 req/s | 3 ms | 3 ms | 4 ms | 7 ms |
| `GET /v1/transactions/{id}` | 20 | ~2,330 req/s | 8 ms | 11 ms | 21 ms | 147 ms |
| `POST /v1/webhooks/transactions` | 20 | ~1,830 req/s | 10 ms | 13 ms | 23 ms | 30 ms |
| `GET /` | 100 | ~7,710 req/s | 13 ms | 14 ms | 20 ms | 26 ms |
| `GET /v1/transactions/{id}` | 100 | ~2,250 req/s | 41 ms | 62 ms | 81 ms | 155 ms |
| `POST /v1/webhooks/transactions` | 100 | ~1,750 req/s | 55 ms | 71 ms | 90 ms | 127 ms |

**Whole server, mixed traffic:** all three endpoints hit at the same time for 10 s by 100
clients (about a third each), while the worker processed the incoming webhooks:

| Endpoint | Requests | Throughput | p50 | p95 | p99 | Max | Errors |
|---|---|---|---|---|---|---|---|
| `GET /` | 10,667 | ~1,070 req/s | 31 ms | 41 ms | 53 ms | 80 ms | 0 |
| `GET /v1/transactions/{id}` | 9,090 | ~910 req/s | 35 ms | 47 ms | 62 ms | 84 ms | 0 |
| `POST /v1/webhooks/transactions` | 8,590 | ~860 req/s | 37 ms | 49 ms | 64 ms | 86 ms | 0 |
| **Total** | **28,347** | **~2,835 req/s** | | | | **86 ms** | **0** |

> **Reading these numbers honestly:** the database was local, so each round trip took ~0.1 ms;
> on Render with Neon (same region) expect ~1–5 ms. `ab` sends the same body every time, so after
> the first request the POST rows measure the duplicate path (no new row written). Inserting
> unique transactions costs somewhat more. `ab` also reuses connections (`-k`). Treat these
> numbers as the app's ceiling, not production figures.

The health check never touches the database, the lookup is a single primary-key read, and the
webhook is a write (an insert), which explains their order. With unique IDs, the Python
client (`loadtest.py`) peaked at ~530–580 req/s at 20 concurrency, but that is the client's limit:
the server process sat at about 6% CPU during those runs, which is why `ab` is used for the
ceiling.

**Rate limiter accuracy:** one client sent requests as fast as possible for 5 seconds with the
default limits on. 64,331 were sent, **1,397 allowed** (expected: 400 burst + 200/s × 5 s =
1,400), and the rest got `429`. Refusals are cheap (~12,000/s), so a flood can't slow the service
down.

### Burst test: how many webhooks can arrive at the same instant

```bash
python scripts/burst_test.py http://localhost:8000 1000
```

`burst_test.py` opens N connections, holds every request at a starting line, releases them all at
once, then reports how many were accepted. Each request has a unique ID, so the number of rows
stored can be compared with the number accepted.

Setup: app on a laptop in India, database on **Neon in Singapore** (~110 ms per round trip), rate
limiting off.

| Burst size | Accepted | Time for all to be answered | Rows stored | Peak DB connections |
|---|---|---|---|---|
| 100 | **100 / 100** | 1.8 s | 100 | |
| 500 | **500 / 500** | 2.4 s | 500 | |
| 1,000 | **1,000 / 1,000** | 3.7 s | 1,000 | |
| 2,000 | **2,000 / 2,000** | 7.3 s | 2,000 | 61 |
| 3,000 | **3,000 / 3,000** | 11.0 s | 3,000 | 74 |
| 5,000 | **5,000 / 5,000** | 18.2 s | 5,000 | 70 |

Stored always equals accepted, with no lost or phantom transactions, and the processed rows were
each processed exactly once. (macOS's default limit of 128 pending TCP connections,
`kern.ipc.somaxconn`, refuses part of a burst above ~1,000 before it reaches the app. These runs
used `sudo sysctl -w kern.ipc.somaxconn=4096`. Linux servers use a much larger default.)

**Database connections.** One instance opens at most 80 connections: the API pool (40 + 20
overflow) plus the worker pool (10 + 10). Requests beyond that wait in line for a pooled
connection instead of opening new ones, so a 5,000-request burst peaked at 74 against Neon's limit
of 901. A request that waits more than 30 s for a connection gets `503` (not stored, so the sender
retries). From this laptop that would start somewhere above ~8,000 simultaneous requests. Neon's
limit only matters when running many instances (~11 at 80 each); beyond that, Neon's built-in
PgBouncer pooler endpoint can handle thousands of client connections.

With rate limiting **on** (defaults), a burst of 1,000 from one client got 417 accepted (the
400-request burst allowance plus refill) and 583 `429 Too Many Requests` responses with
`Retry-After`.

**What made bursts work.** The first run accepted only 120 of 1,000: each webhook made 4 database
round trips (connection check, `BEGIN`, `INSERT`, `COMMIT`) while holding one of 20 pooled
connections, so requests queued for more than 30 s and got `503`. Two changes fixed it:

1. **One round trip per request.** API and worker queries are each a single atomic statement, so
   the connections run in autocommit, and the per-checkout ping was replaced with recycling
   connections every 4 minutes, sooner than Neon's 5-minute idle suspend.
2. **A bigger pool and batch claiming.** The API pool is 40 (+20 overflow). The worker claims up
   to 100 jobs per `UPDATE ... FOR UPDATE SKIP LOCKED` statement instead of one, so it keeps up
   with bursts: it processes 1,000 jobs every ~30 s cycle.

**Processing under load:** 3,000 webhooks were accepted at ~530 req/s. The worker ran 1,000 at a
time, and every processed transaction took exactly 30 s from receipt, on the first attempt, with
no errors.

---

## Local vs live performance

The same code was measured in three setups:

| | Local (laptop) | Laptop app + Neon Singapore | **Live: Render free tier, Singapore** |
|---|---|---|---|
| Where the app runs | laptop, 1 process, a full CPU core | laptop, 1 process | Render **free** instance, **~0.1 CPU**, 512 MB |
| Where the database is | Postgres on the same laptop (~0.1 ms away) | Neon Singapore (~110 ms away) | Neon Singapore (same region) |
| Where requests come from | the same laptop | the same laptop | a laptop in India (~65 ms away) |
| Webhook throughput | **~1,750–1,830 req/s** | — | **~50 req/s** (20 concurrent clients) |
| Lookup throughput | ~2,250–2,330 req/s | — | — |
| Whole server, mixed traffic | **~2,835 req/s** | — | — |
| Single webhook, total response time | ~10 ms | — | **250–350 ms** from India (fresh connection each time) |
| Single webhook, time inside the app (`Server-Timing`) | — | — | **5–18 ms** |
| Single webhook while 200 others are processing | — | — | **264–349 ms** total, **5–6 ms** in the app |
| Webhook latency under sustained load | p95 71 ms, p99 90 ms (100 clients) | — | p50 396 ms, **p95 534 ms**, p99 709 ms (20 clients nonstop) |
| Burst, all at the same instant | — | **5,000 / 5,000 accepted** | **300 / 300 accepted** in 7.6 s |
| Processed exactly once, ~30 s after receipt | ✅ | ✅ | ✅ (900 / 900, 30–31 s) |

**Why live numbers differ: two separate causes, both measured.**

1. **Single requests: almost all of it is network.** The `Server-Timing` header shows a webhook
   takes **5–18 ms inside the app** on Render, with the database 3–4 ms away in the same region.
   The rest of the ~250–350 ms is the trip from India to Singapore (~60–150 ms per round trip
   during testing) plus setting up a new HTTPS connection (about two more round trips) and
   Render's proxy. Background processing does not slow intake: a webhook sent while 200 others
   were processing still took 5–6 ms in the app, because the 30 s waits use no CPU.
2. **Sustained heavy load: the free instance's CPU.** Render's free tier gives ~0.1 CPU, against
   a full core locally. Under nonstop traffic (20 concurrent clients), time inside the app rose
   from ~10 ms to ~200 ms (p50) as requests waited for CPU, and throughput settled around
   30–50 req/s. Running the same Docker image locally capped at `--cpus=0.1` reproduced this
   (~20 req/s, ~600 ms p50 in the app). Turning request logging off in that setup made no
   meaningful difference (20 vs 19 req/s), so the limit is the CPU itself, not logging.

**What it means for the requirements.** Individual webhooks, bursts of hundreds, and webhooks
arriving during background processing all stay under 500 ms, with nearly all of that being network
time. Only nonstop high-rate traffic on the free instance pushes the slowest requests past it.
Nothing is ever lost: every accepted webhook was stored and processed exactly once. A paid instance
(Render Starter ~0.5 CPU, Standard 1 CPU) raises the sustained-load ceiling roughly in line with
the CPU. The rate limit (200 req/s per client) is above what the free instance can serve, so it
never triggered live. It was verified locally (1,397 allowed vs 1,400 expected).

---

## Technical choices

**FastAPI + async SQLAlchemy (asyncpg).** The processing step is almost entirely waiting, so an
async stack lets one process hold over a thousand jobs in flight while still answering webhooks in
milliseconds.

**PostgreSQL as both store and queue.** The transaction row *is* the job (`status = 'PROCESSING'`).
This avoids running a separate broker (Redis/Celery, SQS) and, more importantly, there is no
window where a webhook has been acknowledged but its job exists only in memory. The trade-off: a
dedicated queue scales further and lets workers scale separately from the API. Because accepting
and processing are already separate, moving to one later is straightforward.

**Idempotency enforced by the database.** `transaction_id` is the primary key, and intake uses
`INSERT ... ON CONFLICT DO NOTHING RETURNING`. Checking first and inserting afterwards would let
two simultaneous duplicates both get through; the primary key makes that impossible, however many
instances are running. Duplicates still get `202`, because senders retry on errors and a 4xx/5xx
would just cause more retries.

**Safe concurrent workers: `FOR UPDATE SKIP LOCKED` + leases.** A worker claims up to 100 rows
in one statement that also sets `locked_until` (a lease) and increments `attempts`. Other workers skip
locked rows, and a lease that runs out (the worker crashed) makes the row claimable again. The
final `UPDATE` only applies `WHERE status = 'PROCESSING'`, so finishing is idempotent too.
On shutdown (for example during a deploy), in-flight leases are released straight away instead of
waiting for them to expire.

**Retries and a terminal state.** Failures are retried with exponential backoff (5 s, 10 s, 20 s,
...). After `MAX_ATTEMPTS`, the row becomes `FAILED` with `last_error` saved, so nothing retries
forever and failures stay visible.

**Measurable from outside.** Every response has a `Server-Timing: app;dur=<ms>` header, so time
spent in the app can be told apart from network and proxy time.

**Few database round trips.** Each API request and worker query is one atomic statement run in
autocommit, so there is one round trip per request instead of four (`BEGIN`/`COMMIT` and a
connection check are skipped). Connections are recycled every 4 minutes, before Neon's idle
suspend could kill them. The worker claims jobs in batches of up to 100 per statement.

**No database connection held while waiting.** A job claims its row, commits, waits 30 s holding
no connection, then finishes. Holding connections through the wait would exhaust the pool under
load.

**Bulkhead: separate connection pools.** The worker has its own small pool, so a burst of
background work can never take connections the webhook endpoint needs.

**Event-driven worker.** A new webhook wakes the worker immediately. When idle, it checks only every
10 minutes, for safety. This keeps the free Neon database able to go to sleep instead of being
queried every second, which would use up its monthly compute allowance.

**Data correctness.** Money is `NUMERIC(18,2)` / `Decimal`, never float. Timestamps are
`TIMESTAMPTZ`, always returned in UTC with a `Z`. `CHECK` constraints (amount > 0, valid status)
back up the API validation. A partial index on `created_at WHERE status = 'PROCESSING'` keeps the
worker's lookup fast however large the table grows.

**Rate limiting.** An in-memory token bucket per client IP on `/v1/*` routes (200 req/s, burst
400), returning `429` with `Retry-After`. The limits are generous on purpose: the webhook endpoint
must not turn away legitimate bursts. The health check is exempt.

**Deployment.** Docker image (`python:3.12-slim`, non-root user) on Render; database on Neon. Both
are in Singapore, so each database round trip takes a few milliseconds.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | required | Postgres connection string (Neon's `?sslmode=require` is handled) |
| `PROCESSING_DELAY_SECONDS` | `30` | simulated external call duration |
| `WORKER_CONCURRENCY` | `1000` | maximum jobs in flight per process |
| `WORKER_IDLE_POLL_SECONDS` | `600` | fallback check interval when no webhook has woken the worker |
| `MAX_ATTEMPTS` | `5` | attempts before a transaction is marked `FAILED` |
| `RATE_LIMIT_ENABLED` | `true` | turn per-client rate limiting on or off |
| `RATE_LIMIT_PER_SECOND` | `200` | sustained requests per second per client IP |
| `RATE_LIMIT_BURST` | `400` | short burst allowance per client IP |
| `TEST_DATABASE_URL` | none | used only by the test suite; must differ from `DATABASE_URL` |

---

## Decisions on the spec

- **`GET /v1/transactions/{id}` returns a list** (`[{...}]`), matching the spec's example even though
  it is a single lookup.
- **Amounts may have decimals** (the spec shows both `1500` and `150.50`). Up to 2 decimal places
  are accepted and stored exactly; in JSON they are returned as numbers.
- **A `FAILED` status was added.** The spec lists only `PROCESSING`/`PROCESSED`, but a transaction
  that keeps failing needs a final state rather than retrying forever.
- **Currency codes are normalised** (`" usd "` becomes `USD`) and must be 3 letters.
- **Duplicates return `202` with `"duplicate": true`** rather than an error.

---

## Known limitations and next steps

- **Webhook authenticity is not verified.** Real providers sign each webhook (Razorpay uses an
  HMAC-SHA256 header). The next step is to verify that signature with `hmac.compare_digest` before
  accepting anything.
- **A duplicate with *different* content is ignored**, and the first version wins. It could instead
  be logged or flagged, because a reused ID with a different amount points to an upstream bug.
- **Processing is at-least-once.** If a worker dies after the external call but before marking
  the row `PROCESSED`, the call runs again after the lease expires. Recording the result is
  idempotent; a real external call should be made idempotent too, by passing `transaction_id` as
  the provider's idempotency key.
- **Rate limiting is per process and keyed on client IP.** Behind proxies, IPs can be spoofed via
  `X-Forwarded-For`, and limits are not shared between instances. In production this belongs at
  the edge (an API gateway or WAF) or in a shared store such as Redis.
- **Schema is created with `create_all`.** Fine for one table; a growing schema should use Alembic
  migrations.
- **Free hosting tiers.** Render's free instance sleeps after 15 minutes without traffic, and Neon's
  free database suspends after 5 idle minutes; waking either can push the next request past
  500 ms. An external uptime check (cron-job.org) calls `GET /ready` every 4 minutes, which keeps
  both awake because `/ready` touches the database. Keeping Neon awake around the clock uses about 6
  of its 100 free compute-hours a day, so outside a review period the ping moves to `GET /` (Render
  only). A paid always-on instance and database remove the need for this.
