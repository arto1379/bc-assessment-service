# BC Assessment API Service

FastAPI service exposing `GET /address?address=...` in the format expected by BlueNest's
`BCAssessmentClient`. It checks a SQLite cache first and, on a miss, scrapes
bcassessment.ca with Playwright and parses the page with BeautifulSoup.

For bulk work, `POST /batch` accepts up to 500 addresses, answers what it can from the
cache immediately, and scrapes the rest in the background at a deliberately polite pace,
without slowing live `/address` calls. See [Batch lookups](#batch-lookups-post-batch).

```json
{
  "status": "success",
  "data": {
    "result": {
      "total_value": "$817,000",
      "year_built": "1989",
      "number_of_bed": "2",
      "number_of_baths": "2",
      "description": null,
      "land_size": null,
      "strata_area": "1100",
      "building_storeys": "12"
    },
    "status": 200
  }
}
```

---

## Root cause of the hang / false "Property not found"

The service used to work after a restart and degrade within hours, which is why a systemd
timer restarted it every 2 hours. **A restart only clears two things — process memory and
the cookie jar — and that is exactly what was wrong.** Four defects compounded:

### 1. The scraper never returned to a clean page

```python
async def _ensure_home(page):
    if await page.locator("#rsbSearch").count() > 0:
        return                       # result pages ALSO carry #rsbSearch
    await page.goto(BC_URL, ...)
```

BC Assessment's *result* pages include the same `#rsbSearch` box in the header, so after
the first successful lookup this check short-circuited forever and every later search ran
from the previous property's page instead of the home page.

### 2. That produced silently wrong data, not just hangs

Readiness was detected with a text regex:

```python
"() => /Total value\\s*\\$?[\\d,]+/i.test(document.body.innerText)"
```

Because of defect 1 the DOM still displayed the **previous** property's "Total value $X".
Clicking an autocomplete suggestion navigates asynchronously, so `wait_for_function`
polled and the predicate was *already true from the stale page*. It returned immediately,
`page.content()` captured the old property, and those numbers were cached under the new
address. This is data corruption, and it is why some addresses returned plausible but
incorrect values.

### 3. The browser and context were never recycled

The error path closed the `Page`, but the `BrowserContext` and `Browser` lived for the
whole process. Cookies, storage and Chromium memory accumulated until BC's bot mitigation
began serving a challenge page. From that moment `#rsbSearch` was absent, autocomplete
never appeared, every lookup timed out, and every response became "Property not found" —
permanently, until the next restart.

### 4. The actual hang: rate-limit sleeps held the scrape lock

`lookup_by_address` acquired `_scrape_lock`, and `_scrape_bcassessment` then called
`await bc_rate_limiter.acquire()` **inside** it. On reaching the 500/hour cap, one request
slept for up to an hour *while holding the semaphore*, blocking every queued request.
There was also no ceiling on a whole lookup.

### Why every failure looked identical

`except Exception: return None` collapsed timeouts, challenge pages, network errors and
genuinely unknown addresses into one `None`, which the API reported as
`"Property not found"`. Nobody could tell the scraper was blocked.

### Contributing factor found during this work

`.env` on the server sets `SCRAPE_CACHE_TTL_SECONDS=3600`, so **every cached address was
re-scraped hourly** even though BC Assessment data changes roughly once a year. That
multiplied traffic to bcassessment.ca and materially increased the chance of being
throttled. Set it to `0` (never expire) unless you have a reason not to.

---

## What changed

| Area | Fix |
|---|---|
| **Page state** | A fresh `BrowserContext` + `Page` per lookup, always torn down. A clean context cannot carry stale markup or cookies, which removes defects 1 and 2 structurally rather than patching them. |
| **Browser lifetime** | The browser process is reused but recycled after `BROWSER_MAX_LOOKUPS`, after `BROWSER_RESTART_AFTER_FAILURES` consecutive failures, or on disconnect. |
| **Concurrency** | Rate limiting happens *before* the scrape slot, so a capped-out wait never holds it. A queue-depth guard sheds load with `busy` instead of hanging callers, `SCRAPE_TOTAL_TIMEOUT_MS` caps any single scrape, and the rate-limit wait itself is now bounded: the limiter reports its projected wait without sleeping, and `/address` answers `busy` with `Retry-After` when that wait exceeds `SCRAPE_MAX_PROJECTED_WAIT_SECONDS`. |
| **Timeouts** | Every lookup is wrapped in `SCRAPE_TOTAL_TIMEOUT_MS`; exceeding it forces a browser relaunch. |
| **Failure taxonomy** | `not_found` / `blocked` / `timeout` / `parse_empty` / `error` / `busy`, surfaced as `error_type`. |
| **Retry queue** | Failures go to a `failed_lookups` table with reason, step, URL, snapshot path, attempt count and exponential backoff. Invalid results are never written to the cache. |
| **Alerts** | Blocking sends a Telegram alert with address, type, URL, reason, timestamp, restart status and a suggested action — de-duplicated so a block storm sends one message, plus an all-clear on recovery. |
| **Health** | `/health` reports real scraper state and returns **503** when unhealthy. |
| **Security** | The Telegram token is no longer hardcoded; `.env` is now gitignored. |

The success response and the `"Property not found"` error are byte-for-byte unchanged.
`error_type` is purely additive.

### API surface

| Endpoint | Purpose |
|---|---|
| `GET /address?address=` | Unchanged contract, plus `error_type` on errors and `Retry-After` on `busy` |
| `GET /health` | Scraper health; 503 when unhealthy |
| `GET /stats` | Counters, unresolved failures, cache size, rate-limit usage and batch counters |
| `GET /failures` | Inspect the retry queue (`?due_only=true`, `?include_resolved=true`) |
| `POST /failures/retry` | Re-attempt failures whose backoff elapsed |
| `POST /batch` | Submit up to 500 addresses (see [Batch lookups](#batch-lookups-post-batch)) |
| `GET /batch/{job_id}?offset=0&limit=100` | Job progress and paginated results |
| `DELETE /batch/{job_id}` | Cancel a job's pending items, keeping results already produced |
| `GET /batch/{job_id}/results.csv` | CSV export of a job |

Error responses:

```json
{ "status": "error", "message": "Property not found",            "error_type": "not_found" }
{ "status": "error", "message": "Lookup temporarily unavailable", "error_type": "blocked" }
{ "status": "error", "message": "Lookup timed out",               "error_type": "timeout" }
{ "status": "error", "message": "Service busy, please retry shortly", "error_type": "busy" }
```

`busy` is still HTTP **200** with the same body as before. It now also carries a
`Retry-After` header (seconds). It is returned when more than `SCRAPE_MAX_QUEUE_DEPTH`
requests are queued, or when the rate limiter projects a wait longer than
`SCRAPE_MAX_PROJECTED_WAIT_SECONDS` (default 30 s), for example at the hourly cap.

---

## Batch lookups (`POST /batch`)

Batch moves bulk waiting out of open HTTP requests and into a persisted job that the caller
polls, or is told about by webhook. It is a thin layer over the same lookup path as
`/address`: the same cache, a fresh browser context per lookup, block detection, the rule
that invalid results are never cached, and the `failed_lookups` record all apply.

**Off by default.** Set `BATCH_ENABLED=true` and at least one key in `BATCH_API_KEYS`.
Every `/batch` call needs an `X-API-Key` header.

### Submit

```bash
curl -s -X POST localhost:8011/batch \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"addresses": ["7009 TISDALL ST VANCOUVER", "12 EXAMPLE AVE BURNABY"],
       "client_ref": "import-2026-11-09"}'
```

```json
{
  "status": "success",
  "data": {
    "job_id": "3f9c0d2a1b7e4c55",
    "state": "queued",
    "total": 2,
    "unique": 2,
    "cached": 1,
    "pending_scrape": 1,
    "estimated_seconds": 11
  }
}
```

- 1 to `BATCH_MAX_ADDRESSES` (500) addresses, each non-blank and at most 200 characters.
- Addresses are normalised with the cache key (upper-cased, whitespace collapsed) and
  de-duplicated. Each unique address is resolved once and its result repeated at every
  original index.
- **Cache-first:** addresses already in the cache, including the not-found cache, are
  answered at submit time without touching the scraper. A fully cached job comes back
  `completed` straight away (500 cached addresses take well under 5 s).
- `estimated_seconds` is a lower bound: the scrapes queued across all active jobs at the
  batch rate (one per `max(4 s, 3600 / 350)` ≈ 10.3 s), plus any wait the limiter already
  projects.
- Optional `callback_url` (https, allowlisted host) and `client_ref` (≤ 200 characters,
  echoed back).

### Status and results

`GET /batch/{job_id}?offset=0&limit=100` (limit ≤ 500). Each item carries the same `result`
object as `/address`, plus `source` (`cache` or `scrape`) and `error_type`.

```json
{
  "status": "success",
  "data": {
    "job_id": "3f9c0d2a1b7e4c55",
    "state": "running",
    "client_ref": "import-2026-11-09",
    "total": 2,
    "progress": { "done": 1, "not_found": 0, "failed": 0, "pending": 1, "cancelled": 0 },
    "paused_reason": null,
    "created_at": "2026-11-13T01:51:30.101+00:00",
    "completed_at": null,
    "items": [
      { "index": 0, "address": "7009 TISDALL ST VANCOUVER", "state": "done",
        "source": "cache", "error_type": null,
        "result": { "total_value": "$817,000", "year_built": "1989", "...": "same fields as /address" } },
      { "index": 1, "address": "12 EXAMPLE AVE BURNABY", "state": "pending",
        "source": null, "error_type": null, "result": null }
    ],
    "next_offset": null
  }
}
```

**Job states:** `queued` → `running` → `completed`, with `paused` when BC is blocking and
`cancelled` after `DELETE`. **Item states:** `pending`, `done`, `not_found`, `failed`,
`cancelled`. A `failed` item's `error_type` is one of `blocked`, `timeout`, `parse_empty`,
`error`. A `pending` item that is waiting to retry shows the last `error_type` it hit.
`busy` never appears on an item: the worker waits instead of shedding load.

`DELETE /batch/{job_id}` cancels pending items and keeps every result already produced. It
is idempotent: cancelling a finished job returns its current state. If a scrape is in flight
when you cancel, its result still goes to the cache but not to the cancelled job.

`GET /batch/{job_id}/results.csv` exports one row per original index (`index, address,
state, source, error_type`, then the `/address` result fields). Cells that would start a
spreadsheet formula are prefixed with `'`.

**Errors:** `400` invalid body (empty, oversize, blank or over-long entry, bad
`callback_url`), `401` missing or wrong `X-API-Key`, `404` unknown or purged job, `429` more
than `BATCH_MAX_ACTIVE_JOBS` (3) active jobs or `BATCH_MAX_PENDING_ITEMS` (2,000) pending
items service-wide, `503` `BATCH_ENABLED` is false. Error bodies use the usual
`{"status": "error", "message": ..., "error_type": ...}` shape.

### How batch work is paced

- **Live requests first.** Scraping is still single-flight. The old `Semaphore(1)` is now a
  gate where a waiting `/address` request always gets the next slot, and the next rate-limit
  token, ahead of any batch item. A scrape in progress is never interrupted, so a live
  request waits behind at most one scrape.
- **Batch rate share.** Batch scrapes may use at most `BATCH_HOURLY_SHARE` (0.7, i.e. 350 of
  the 500/hour cap). The 4 s minimum spacing is unchanged, so batch is never faster than a
  client loop.
- **No sleeping holding a lock.** The worker sleeps only in its own loop, never while it
  holds the scrape slot.
- **Two jobs, one scrape.** Pending items for the same address in different jobs share one
  scrape, and the worker re-checks the cache before every scrape (a live call may have
  filled it).

### Failures, blocking and restarts

- `timeout`, `error` and `blocked` retry per item with exponential backoff (30 s, 60 s, …) up
  to `BATCH_ITEM_MAX_ATTEMPTS` (3), then the item becomes `failed`. `not_found` and
  `parse_empty` are final. Every scrape failure is also recorded in `failed_lookups`, so
  `/failures` stays complete.
- **Circuit breaker.** After `BATCH_PAUSE_AFTER_BLOCKED` (3) consecutive `blocked` results,
  every active job is `paused` with a `paused_reason`, and one Telegram alert is sent
  (de-duplicated by `ALERT_MIN_INTERVAL_SECONDS`). The worker then probes one item at a time
  at growing intervals (30 s, doubling, up to 15 min). A probe that is still blocked costs
  the item no attempt. Jobs resume, with a "resumed" alert, when a probe gets an answer from
  BC (a result or a genuine not-found) or when any live `/address` lookup has succeeded
  since the pause. A probe that times out or errors does not resume them; the interval
  keeps growing. Jobs submitted during a pause start
  `paused`. Per-address `not_found` and `blocked` alerts are not sent for batch items, so a
  500-address import cannot flood Telegram.
- **Restarts.** Results are written per item. On startup, items that were in flight return
  to `pending`, paused jobs resume, and the worker carries on. A restart loses at most the
  one scrape in flight, and no completed item is scraped again.
- **Retention.** Finished jobs older than `BATCH_RETENTION_DAYS` (7) are purged hourly.
  Active jobs are never purged.

### Webhook

If the job has a `callback_url`, on `completed` or `cancelled` the service POSTs:

```json
{"job_id":"3f9c0d2a1b7e4c55","state":"completed","total":2,"progress":{"done":2,"not_found":0,"failed":0,"pending":0,"cancelled":0}}
```

with `X-BC-Signature: sha256=<hex HMAC-SHA256 of the raw body, keyed with
BATCH_WEBHOOK_SECRET>`. It carries no property data; fetch that with `GET /batch/{job_id}`.
To verify on the receiving side:

```python
import hashlib, hmac
expected = "sha256=" + hmac.new(SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
ok = hmac.compare_digest(expected, request.headers["X-BC-Signature"])
```

Server-side request forgery guards: https only; the host must be in
`BATCH_CALLBACK_ALLOWED_HOSTS`; URLs with credentials are rejected; redirects are never
followed; 5 s timeout; and hosts that resolve to private, loopback or link-local addresses
are refused. A non-2xx reply is retried 3 times with backoff (2 s, 4 s, 8 s). Webhooks are
enabled only when both the allowlist and the secret are set. Otherwise a submit with a
`callback_url` gets a 400 telling the caller to poll.

### Rollout

1. Release the rate-limiter change with `BATCH_ENABLED=false`. Watch `/stats` → `rate_limit`
   for a day.
2. Run `python scripts/setup_db.py` (safe to re-run) and release the batch code, still
   disabled.
3. Enable on staging with `LOOKUP_PROVIDER=mock`, then on live with 10, 100 and finally 500
   addresses, checking `/failures` and `/stats` → `batch` for `blocked` between steps.
4. Issue BlueNest an API key (`BATCH_API_KEYS`) and point its bulk flows at `/batch`.

### Answers to the PRD's open questions

- **What HTTP status does `busy` return?** 200, with the error body. Batch keeps that and
  only adds `Retry-After`.
- **Does the cache key normalise case, spacing and street-type abbreviations?** Case and
  whitespace, yes. Abbreviations, no: `ST` and `STREET` are different keys, both in
  `/address` and in batch de-duplication. Batch deliberately reuses the existing key so a
  batch result and an `/address` result for the same input always share one cache entry.
- Batch sizes, whether polling is enough, and bcassessment.ca's terms of use are still open,
  and need BlueNest and the business to answer.

---

## Setup

### Linux prerequisites

```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git curl
sudo apt install -y nginx        # optional: reverse proxy
```

### Install

```bash
git clone <repo> && cd bc-assessment-service
python3 -m venv SCRAP
source SCRAP/bin/activate
pip install -r requirements.txt
playwright install chromium
playwright install-deps chromium
```

If `install-deps` fails:

```bash
sudo apt install -y libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libgbm1 libasound2
```

### Configure

```bash
cp .env.example .env
```

Then edit `.env`. **`.env` is gitignored — never commit it.**

### Database

```bash
python scripts/setup_db.py
```

Creates `assessment_cache`, `failed_lookups`, `batch_jobs` and `batch_items`, and puts the
file in WAL mode. Safe to re-run: it is also the migration that adds the newer tables to an
existing database without touching cached results.

### Run

```bash
python -m app.main
# or
uvicorn app.main:app --host 0.0.0.0 --port 8011
```

---

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `HOST` / `PORT` | `0.0.0.0` / `8011` | Bind address |
| `LOOKUP_PROVIDER` | `live` | `live` scrapes BC; `mock` serves fixtures (tests) |
| `DATABASE_PATH` | `data/bc_assessment.db` | SQLite file |
| `SCRAPE_CACHE_TTL_SECONDS` | `0` | Cache lifetime; `0` = never expire (**recommended**) |
| `NOT_FOUND_CACHE_TTL_SECONDS` | `86400` | How long a genuine not-found is remembered |
| `BC_RATE_LIMIT_MAX` | `15` | Max scrapes per short window |
| `BC_RATE_LIMIT_WINDOW_SECONDS` | `60` | Short window |
| `BC_RATE_LIMIT_HOURLY_MAX` | `500` | Hourly cap |
| `BC_RATE_LIMIT_HOURLY_WINDOW_SECONDS` | `3600` | Hourly window |
| `BC_RATE_LIMIT_MIN_INTERVAL_SECONDS` | `4` | Minimum spacing between BC requests |
| `SCRAPE_WARMUP_ON_START` | `true` | Pre-launch the browser at boot |
| `SCRAPE_AUTOCOMPLETE_TIMEOUT_MS` | `8000` | Wait for suggestions |
| `SCRAPE_RESULT_TIMEOUT_MS` | `25000` | Wait for the property page |
| `SCRAPE_TOTAL_TIMEOUT_MS` | `60000` | Ceiling on one whole lookup |
| `SCRAPE_MAX_QUEUE_DEPTH` | `20` | Queued requests before shedding load |
| `SCRAPE_MAX_PROJECTED_WAIT_SECONDS` | `30` | `/address` returns `busy` + `Retry-After` above this projected rate-limit wait |
| `BROWSER_MAX_LOOKUPS` | `200` | Recycle the browser after N lookups (`0` = never) |
| `BROWSER_RESTART_AFTER_FAILURES` | `3` | Recycle after N consecutive failures |
| `UNHEALTHY_AFTER_FAILURES` | `5` | Failures before `/health` returns 503 |
| `SAVE_HTML_SNAPSHOTS` | `true` | Save HTML for blocked/unexpected pages |
| `SNAPSHOT_DIR` / `SNAPSHOT_KEEP` | `data/snapshots` / `50` | Snapshot location and retention |
| `TELEGRAM_BOT_TOKEN` | _(empty)_ | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_IDS` | _(empty)_ | Comma-separated chat IDs |
| `STATS_MILESTONE_COUNT` | `100` | Reset counters and alert at this many calls |
| `ALERT_MIN_INTERVAL_SECONDS` | `300` | Minimum gap between repeat alerts of one type |
| `BATCH_ENABLED` | `false` | Feature flag for `/batch` (503 when off) |
| `BATCH_API_KEYS` | _(empty)_ | Comma-separated keys accepted in `X-API-Key`; empty locks everyone out |
| `BATCH_MAX_ADDRESSES` | `500` | Addresses per job |
| `BATCH_MAX_ACTIVE_JOBS` | `3` | Active jobs before 429 |
| `BATCH_MAX_PENDING_ITEMS` | `2000` | Pending items service-wide before 429 |
| `BATCH_HOURLY_SHARE` | `0.7` | Fraction of `BC_RATE_LIMIT_HOURLY_MAX` batch may use |
| `BATCH_ITEM_MAX_ATTEMPTS` | `3` | Attempts per item |
| `BATCH_PAUSE_AFTER_BLOCKED` | `3` | Consecutive `blocked` results before jobs pause |
| `BATCH_RETENTION_DAYS` | `7` | Age at which finished jobs are purged |
| `BATCH_WEBHOOK_SECRET` | _(empty)_ | HMAC key for webhook signatures; empty disables webhooks |
| `BATCH_CALLBACK_ALLOWED_HOSTS` | _(empty)_ | Allowed webhook hosts (comma-separated); empty disables webhooks |

> **Alerts require configuration now.** The bot token used to be a hardcoded default in
> `app/config.py`. It has been removed, so Telegram alerts stay silent until
> `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_IDS` are set in `.env`.

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

153 tests covering parsing, block detection, cache behaviour (including that invalid
results are never cached, and that concurrent reads from many threads never cross rows),
the retry queue and negative cache, browser recycling policy,
alert de-duplication, the full API surface, and batch lookups. They run against the `mock`
provider, so no browser and no network are required.

`tests/test_concurrency.py` is worth calling out: `lookup_by_address` branches to the mock
provider *before* `_run_scrape`, so the queue guard, the total-timeout ceiling, the rate
limiter and the priority gate get no coverage from the rest of the suite — nor from
`load_test.py` run against a mock server. Those tests drive `_run_scrape` directly with a
stubbed scrape and a fake clock: a live request overtakes 50 queued batch items, `busy` and
`Retry-After` appear when the projected wait exceeds the threshold, batch never exceeds its
hourly share, and no lock is held while sleeping.

Batch has three more files: `test_batch_api.py` (validation, auth, de-duplication,
cache-first, pagination, cancel, limits, CSV, and the byte-for-byte `/address` check),
`test_batch_worker.py` (retries and backoff, circuit breaker pause and resume, restart
mid-job, scrape sharing, purge, the 500-cached benchmark), and `test_webhook.py` (signature,
allowlist, refused redirects, retries, private-address refusal).

### Load / reproduction harness

```bash
python scripts/load_test.py --base-url http://localhost:8011 --mode all
python scripts/load_test.py --mode stale        # stale-result regression check
python scripts/load_test.py --mode soak --duration 14400   # 4-hour soak
```

Modes: `unique`, `cache`, `unknown`, `malformed`, `concurrent`, `stale`, `soak`, `batch`,
`mixed`, `all`. It reports latency percentiles and an outcome breakdown.

`--mode stale` is the direct regression test for defect 2: it asserts that distinct
addresses never return the same `total_value`.

The batch modes need a server started with `BATCH_ENABLED=true` and a key:

```bash
python scripts/load_test.py --mode batch --api-key KEY --batch-size 300
python scripts/load_test.py --mode mixed --api-key KEY
```

`--mode batch` runs a job to completion, applies the same stale-result check across the
batch, and checks that a re-submit is answered entirely from cache. `--mode mixed` measures
`/address` p95 idle and again while a batch runs, and fails above 1.5× or on any `busy`.
Without `--addresses-file` both submit synthetic addresses, which suits the mock provider.
Against the live provider, pass a file of real addresses. `--mode all` runs the batch modes
only when a key is given.

---

## Production (systemd)

`/etc/systemd/system/bc-assessment.service`:

```ini
[Unit]
Description=BC Assessment API
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/bc-assessment-service
Environment=PATH=/root/bc-assessment-service/SCRAP/bin
ExecStart=/root/bc-assessment-service/SCRAP/bin/uvicorn app.main:app --host 0.0.0.0 --port 8011
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bc-assessment
sudo systemctl status bc-assessment
```

### Removing the 2-hour restart timer

The timer was a workaround for the defects above and is no longer the fix. After
deploying and soaking successfully:

```bash
sudo systemctl disable --now bc-assessment-restart.timer
systemctl list-timers | grep bc-assessment    # should be empty
```

`Restart=always` remains, so the service still recovers from a genuine crash.

### Operating

```bash
sudo journalctl -u bc-assessment -f            # logs
curl localhost:8011/health                     # scraper health (503 = unhealthy)
curl localhost:8011/failures                   # retry queue
curl -X POST localhost:8011/failures/retry     # retry what is due
curl localhost:8011/stats                      # rate_limit tiers, batch jobs/items, paused?
curl -H "X-API-Key: $KEY" localhost:8011/batch/<job_id>            # one job
curl -X DELETE -H "X-API-Key: $KEY" localhost:8011/batch/<job_id>  # cancel a runaway job
sudo systemctl restart bc-assessment           # restart (batch jobs resume)
```

Batch log lines carry `job_id=` and `idx=`, so `journalctl -u bc-assessment | grep job_id=<id>`
traces one job.

Every failure logs address, failure type, step, URL and reason, so a bad lookup can be
traced without reproducing it.

---

## Connecting BlueNest

```bash
BC_ASSESSMENT_API_URL=http://YOUR_HOST:8011/address
BC_ASSESSMENT_TIMEOUT=30
```

Production check:

```bash
curl -G "http://getplutus.solutions/bc-assessment/address" \
  --data-urlencode "address=7009 TISDALL ST VANCOUVER"
```

---

## Known limitations

- Scraping remains single-flight by design, to avoid overloading BC Assessment. Throughput
  is bounded by `BC_RATE_LIMIT_MIN_INTERVAL_SECONDS`.
- ~~A capped-out rate-limit wait is not bounded.~~ **Fixed.** The limiter used to sleep
  inside its own `asyncio.Lock`, outside `SCRAPE_TOTAL_TIMEOUT_MS`, so at the hourly cap a
  request could block for up to an hour. Limiters now report `projected_wait()` without
  sleeping and never hold a lock while waiting. `/address` answers `busy` with
  `Retry-After` within about a second once the projected wait exceeds
  `SCRAPE_MAX_PROJECTED_WAIT_SECONDS`. A live caller now waits at most that threshold for
  a rate-limit slot, then behind the scrape in progress and any earlier live requests,
  never behind batch work.
- Batch de-duplication uses the existing cache key, which normalises case and whitespace
  but not street-type abbreviations (`ST` vs `STREET` are scraped separately).
- A webhook in flight at shutdown is lost (deliveries are not persisted). The job's result
  is not: callers can always poll `GET /batch/{job_id}`.
- The webhook's private-address check resolves the host just before sending. The host
  allowlist is the primary guard; the check does not defend against an allowlisted host
  whose DNS is changed between the check and the request.
- Under sustained live saturation, batch jobs slow down by design (live goes first). They
  lose nothing and finish once live traffic eases.
- A fresh context per lookup costs roughly 200–400 ms, which is negligible against the 4 s
  minimum request spacing and buys correctness.
- Playwright is still used for every live lookup. An httpx fast path was considered and
  deliberately deferred: the PRD requires that optimization never reduce reliability, and
  the rate-limit floor means it would save little in practice.
- `not_found` classification is heuristic — if BC's autocomplete returns nothing while the
  page is otherwise healthy, the address is treated as unknown. Genuine blocks are caught
  by explicit marker/status detection first.
