# Transaction-Webhook-Service

Webhook service that accepts payment transactions instantly (202) and processes them reliably in the background, with idempotency and crash recovery. FastAPI + PostgreSQL.

**Live API:** _deploying, the URL will be added here_
**Interactive docs:** `<live-url>/docs`

---

## Contents

- [Try it](#try-it)
- [How it works](#how-it-works)
- [API](#api)
- [Running locally](#running-locally)
- [Testing](#testing)
- [Technical choices](#technical-choices)
- [Configuration](#configuration)
- [Decisions on the spec](#decisions-on-the-spec)
- [Known limitations and next steps](#known-limitations-and-next-steps)

---

## Try it

```bash
BASE=<live-url>   # or http://localhost:8000

# 1. Send a webhook: answered immediately with 202
curl -i -X POST $BASE/v1/webhooks/transactions \
  -H "Content-Type: application/json" \
  -d '{"transaction_id":"txn_abc123def456","source_account":"acc_user_789","destination_account":"acc_merchant_456","amount":1500,"currency":"INR"}'

# 2. Send the same webhook again: still 202, flagged as a duplicate, not processed twice
#    {"status":"ACCEPTED","transaction_id":"txn_abc123def456","duplicate":true}

# 3. Check status: PROCESSING now, PROCESSED about 30 seconds later
curl $BASE/v1/transactions/txn_abc123def456

# Health check
curl $BASE/
```

---

## How it works

```
 Payment provider ──POST /v1/webhooks/transactions──▶ API
                                                     1. validate the body (422 if invalid)
                                                     2. INSERT ... ON CONFLICT (transaction_id) DO NOTHING
                                                     3. reply 202, typically in ~10 ms

 Background worker (runs inside the same app, started at startup)
                                                     a. claim a row:  FOR UPDATE SKIP LOCKED + a time-limited lease
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

## Testing

### Automated tests

```bash
# Needs a separate, disposable database; the suite refuses to run against DATABASE_URL.
# Set TEST_DATABASE_URL in .env, then:
pytest -v
```

The tests use a 2-second processing delay instead of 30 seconds so the suite runs quickly. Every
test starts with an empty table.

**27 tests**, grouped by what they prove:

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
| Health | `GET /` returns `HEALTHY` with a UTC timestamp |

### Manual test against the live service

Use the commands in [Try it](#try-it). To see duplicate protection under concurrency:

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
measurement). Every request succeeded in every run.

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
webhook is an insert inside a transaction, which explains their order. With unique IDs, the Python
client (`loadtest.py`) peaked at ~530–580 req/s at 20 concurrency, but that is the client's limit:
the server process sat at about 6% CPU during those runs, which is why `ab` is used for the
ceiling.

**Rate limiter accuracy:** one client sent requests as fast as possible for 5 seconds with the
default limits on. 64,331 were sent, **1,397 allowed** (expected: 400 burst + 200/s × 5 s =
1,400), and the rest got `429`. Refusals are cheap (~12,000/s), so a flood can't slow the service
down.

**Processing under load:** 3,000 webhooks were accepted at ~530 req/s. The worker ran 1,000 at a
time, and every processed transaction took exactly 30 s from receipt, on the first attempt, with
no errors.

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

**Safe concurrent workers: `FOR UPDATE SKIP LOCKED` + leases.** A worker claims a row in one
statement that also sets `locked_until` (a lease) and increments `attempts`. Other workers skip
locked rows, and a lease that runs out (the worker crashed) makes the row claimable again. The
final `UPDATE` only applies `WHERE status = 'PROCESSING'`, so finishing is idempotent too.
On shutdown (for example during a deploy), in-flight leases are released straight away instead of
waiting for them to expire.

**Retries and a terminal state.** Failures are retried with exponential backoff (5 s, 10 s, 20 s,
...). After `MAX_ATTEMPTS`, the row becomes `FAILED` with `last_error` saved, so nothing retries
forever and failures stay visible.

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
- **Free hosting tiers.** Render's free instance sleeps after 15 minutes without traffic, so an
  external uptime check pings `GET /` to keep it awake. Neon's free database also sleeps when
  idle, so the first request after a quiet period can be slower. A paid always-on instance
  removes both.
