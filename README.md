# BC Assessment API Service

FastAPI service exposing `GET /address?address=...` in the format expected by BlueNest's
`BCAssessmentClient`. It checks a SQLite cache first and, on a miss, scrapes
bcassessment.ca with Playwright and parses the page with BeautifulSoup.

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
| **Concurrency** | Rate limiting happens *before* the scrape lock, so a capped-out sleep no longer holds the semaphore. A queue-depth guard sheds load with `busy` instead of hanging callers, and `SCRAPE_TOTAL_TIMEOUT_MS` caps any single scrape. Those last two are what actually stop a wedge — see the caveat below. |
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
| `GET /address?address=` | Unchanged contract, plus `error_type` on errors |
| `GET /health` | Scraper health; 503 when unhealthy |
| `GET /stats` | Counters, plus unresolved failures and cache size |
| `GET /failures` | Inspect the retry queue (`?due_only=true`, `?include_resolved=true`) |
| `POST /failures/retry` | Re-attempt failures whose backoff elapsed |

Error responses:

```json
{ "status": "error", "message": "Property not found",            "error_type": "not_found" }
{ "status": "error", "message": "Lookup temporarily unavailable", "error_type": "blocked" }
{ "status": "error", "message": "Lookup timed out",               "error_type": "timeout" }
```

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

Creates `assessment_cache` and `failed_lookups`. Safe to re-run: it is also the migration
that adds `failed_lookups` to an existing database without touching cached results.

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
| `BC_RATE_LIMIT_MIN_INTERVAL_SECONDS` | `4` | Minimum spacing between BC requests |
| `SCRAPE_WARMUP_ON_START` | `true` | Pre-launch the browser at boot |
| `SCRAPE_AUTOCOMPLETE_TIMEOUT_MS` | `8000` | Wait for suggestions |
| `SCRAPE_RESULT_TIMEOUT_MS` | `25000` | Wait for the property page |
| `SCRAPE_TOTAL_TIMEOUT_MS` | `60000` | Ceiling on one whole lookup |
| `SCRAPE_MAX_QUEUE_DEPTH` | `20` | Queued requests before shedding load |
| `BROWSER_MAX_LOOKUPS` | `200` | Recycle the browser after N lookups (`0` = never) |
| `BROWSER_RESTART_AFTER_FAILURES` | `3` | Recycle after N consecutive failures |
| `UNHEALTHY_AFTER_FAILURES` | `5` | Failures before `/health` returns 503 |
| `SAVE_HTML_SNAPSHOTS` | `true` | Save HTML for blocked/unexpected pages |
| `SNAPSHOT_DIR` / `SNAPSHOT_KEEP` | `data/snapshots` / `50` | Snapshot location and retention |
| `TELEGRAM_BOT_TOKEN` | _(empty)_ | From [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_IDS` | _(empty)_ | Comma-separated chat IDs |
| `STATS_MILESTONE_COUNT` | `100` | Reset counters and alert at this many calls |
| `ALERT_MIN_INTERVAL_SECONDS` | `300` | Minimum gap between repeat alerts of one type |

> **Alerts require configuration now.** The bot token used to be a hardcoded default in
> `app/config.py`. It has been removed, so Telegram alerts stay silent until
> `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_IDS` are set in `.env`.

---

## Testing

```bash
pip install -r requirements-dev.txt
pytest
```

70 tests covering parsing, block detection, cache behaviour (including that invalid
results are never cached), the retry queue and negative cache, browser recycling policy,
alert de-duplication, and the full API surface. They run against the `mock` provider, so
no browser and no network are required.

`tests/test_concurrency.py` is worth calling out: `lookup_by_address` branches to the mock
provider *before* `_run_scrape`, so the queue guard, the total-timeout ceiling and the
rate limiter get no coverage from the rest of the suite — nor from `load_test.py` run
against a mock server. Those tests drive `_run_scrape` directly with a stubbed scrape.

### Load / reproduction harness

```bash
python scripts/load_test.py --base-url http://localhost:8011 --mode all
python scripts/load_test.py --mode stale        # stale-result regression check
python scripts/load_test.py --mode soak --duration 14400   # 4-hour soak
```

Modes: `unique`, `cache`, `unknown`, `malformed`, `concurrent`, `stale`, `soak`, `all`.
It reports latency percentiles and an outcome breakdown.

`--mode stale` is the direct regression test for defect 2: it asserts that distinct
addresses never return the same `total_value`.

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
sudo systemctl restart bc-assessment           # restart
```

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

- Scraping remains single-flight (`Semaphore(1)`) by design, to avoid overloading BC
  Assessment. Throughput is bounded by `BC_RATE_LIMIT_MIN_INTERVAL_SECONDS`.
- **A capped-out rate-limit wait is not bounded.** `BCRateLimiter.acquire()` sleeps inside
  its own `asyncio.Lock`, so waiters still serialise — moving it outside `_scrape_lock`
  changed *which* lock they queue on, not that they queue. And because `acquire()` sits
  outside the `asyncio.wait_for` in `_run_scrape`, `SCRAPE_TOTAL_TIMEOUT_MS` does not cover
  it: at the hourly cap a request can block for up to an hour. Callers beyond
  `SCRAPE_MAX_QUEUE_DEPTH` at least get a fast `busy` rather than hanging. Returning `busy`
  once the projected wait exceeds a threshold would be the real fix.
- A fresh context per lookup costs roughly 200–400 ms, which is negligible against the 4 s
  minimum request spacing and buys correctness.
- Playwright is still used for every live lookup. An httpx fast path was considered and
  deliberately deferred: the PRD requires that optimization never reduce reliability, and
  the rate-limit floor means it would save little in practice.
- `not_found` classification is heuristic — if BC's autocomplete returns nothing while the
  page is otherwise healthy, the address is treated as unknown. Genuine blocks are caught
  by explicit marker/status detection first.
