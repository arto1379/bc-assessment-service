# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A FastAPI service that returns BC Assessment property data for a formatted address. It
checks a SQLite cache, and on a miss scrapes bcassessment.ca with Playwright and parses
the page with BeautifulSoup. It is consumed by BlueNest's `BCAssessmentClient` and runs on
a Linux VPS under systemd behind nginx.

## Commands

The virtualenv is conventionally named `SCRAP/` (not `.venv/`), matching the systemd unit
and README.

```bash
python3 -m venv SCRAP && source SCRAP/bin/activate
pip install -r requirements.txt
playwright install chromium && playwright install-deps chromium
cp .env.example .env
python scripts/setup_db.py          # idempotent; also the migration
```

```bash
pip install -r requirements-dev.txt
pytest                                              # full suite
pytest tests/test_api.py                            # one file
pytest tests/test_api.py::TestFailures              # one class
pytest "tests/test_cache.py::test_result_without_total_value_is_never_cached"
pytest -k "negative_cache" -v                       # by keyword
```

```bash
python -m app.main                                  # honours HOST/PORT from .env
uvicorn app.main:app --host 0.0.0.0 --port 8011
```

Reproduction / load harness (needs a running server; `--mode` defaults to `all`):

```bash
python scripts/load_test.py --base-url http://localhost:8011 --mode all
python scripts/load_test.py --mode stale                   # stale-result regression
python scripts/load_test.py --mode soak --duration 14400   # 4-hour soak
```

On this Windows dev machine `python` is not on PATH — use the `py` launcher (Python 3.13).
Playwright is only needed for `LOOKUP_PROVIDER=live`; the test suite never launches a
browser.

## Architecture

Request flow, which spans several modules:

```
main.py  (routes, maps FailureType -> message + error_type)
  -> services/lookup.py   orchestration: cache -> negative cache -> provider -> record
       -> services/browser.py   BrowserManager: owns the Browser, yields a fresh context
       -> services/scraper.py   named Playwright steps, block detection, parsing
       -> db/cache.py           assessment_cache  (successes only)
       -> db/failures.py        failed_lookups    (failures + negative cache)
       -> services/stats.py     counters, de-duplicated alerts -> telegram_notify.py
```

`db/connection.py` owns the single shared SQLite connection **and** the schema DDL, so the
runtime and `scripts/setup_db.py` can never drift. The schema is `CREATE TABLE IF NOT
EXISTS` throughout and is re-applied on connect, which is what makes `setup_db.py` double
as the migration for databases predating `failed_lookups`.

### Invariants — violating these reintroduces the production outage

This service previously hung and returned false "Property not found" until a systemd timer
restarted it every 2 hours. The README documents the full root cause. The fixes rely on
these rules:

1. **Never reuse a `Page` or `BrowserContext` across lookups.** `browser_manager.lookup_context()`
   is the only sanctioned way to obtain a page, and it always tears the context down.
   Reusing a page left the previous property's DOM in place; the readiness check then
   matched that stale markup and cached the *previous* property's values under the *new*
   address. A fresh context makes this impossible rather than merely unlikely.
2. **Acquire `bc_rate_limiter` before `_scrape_lock`, never inside it.** Rate-limit waits
   can last up to an hour at the hourly cap; holding the semaphore through one blocked
   every queued request. This ordering is the fix for the hang.
3. **Only valid results reach `assessment_cache`.** Anything without `total_value` is a
   failure and belongs in `failed_lookups`. `cache_set` enforces this defensively.
4. **Only `FailureType.NOT_FOUND` may be negatively cached.** Caching `blocked`/`timeout`/
   `error` would freeze a transient outage into a permanent wrong answer.
5. **`/address` response shape is a contract** with BlueNest. Success and the exact string
   `"Property not found"` must stay byte-for-byte identical; `error_type` is additive only.

### Failure taxonomy

`FailureType` in `schemas.py` is the spine of the design — the original code collapsed
every failure into a bare `None`, making a blocked scraper indistinguishable from a
nonexistent address. Every failure path must return a classified `LookupOutcome` carrying
reason, step and URL. `scraper.py` labels each step (`goto_home`, `fill_search`,
`await_autocomplete`, `click_suggestion`, `await_result_nav`, `extract_html`, `parse`) so
failures report where they broke.

The `not_found` vs `timeout` distinction lives in `_classify_missing_autocomplete`: if no
suggestion appeared but the page is otherwise healthy (search box present, text still in
it, no block markers), the address is genuinely unknown; otherwise the scraper is at
fault. Block detection (`detect_block`) checks HTTP status, off-site redirects, and
content markers, and runs *before* falling back to a timeout classification.

### Configuration

`app/config.py` exposes a module-level `settings` singleton built from `.env` at import
time. Because it is read at call time (not captured), tests override behaviour with
`monkeypatch.setattr(settings, "field", value)`. After changing `database_path` you must
call `connection.reset_for_tests()` — the connection is cached in a module global.

Telegram credentials must come from `.env` only; the defaults are empty strings and a
regression test asserts they carry no credentials.

### Testing

`tests/conftest.py` has two autouse fixtures giving every test a throwaway SQLite database
and silenced/reset alerting. The `client` fixture sets `LOOKUP_PROVIDER=mock`, so the full
request path runs without Playwright or network.

The mock provider lives in `lookup.py` (`_mock_lookup`) and derives values from a hash of
the address, so distinct addresses yield distinct figures. Magic tokens in an address
drive behaviour: `NOWHERE`/`INVALID`/`DOESNOTEXIST` produce `not_found`, `BLOCKED`
produces `blocked`.

Because the mock provider cannot exhibit the stale-page defect, the real regression test
for it is `scripts/load_test.py --mode stale` against a live server, which asserts that
distinct addresses never return the same `total_value`.
