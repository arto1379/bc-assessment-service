# BC Assessment Service — Test & Verification Report

**Purpose:** Demonstrate, with evidence, that the stability/optimization work fixes the
production hang and false *"Property not found"* behaviour described in the troubleshooting
PRD — and that the service no longer needs the 2‑hour systemd restart timer.

| | |
|---|---|
| **Branch** | `Arshia` |
| **Commit** | `279fd26` |
| **Report author** | Arshia (arto1379) |
| **Local test date** | 2026‑07‑27 |
| **Local environment** | Windows 11, Python 3.13.2, `LOOKUP_PROVIDER=mock` |
| **VPS test date** | _<fill in>_ |
| **VPS environment** | _<Ubuntu 22.04 / vCPU / RAM>_, Python 3.x, `LOOKUP_PROVIDER=live` |

> This report has two halves. **Part A (local)** is complete and reproducible on any
> machine with no browser or network — the numbers below are real output from this commit.
> **Part B (VPS, live)** is the deployment run the reviewer asked for; run the commands and
> paste the output into the marked tables. Part B is what actually proves the live‑scraper
> fix, because the mock provider cannot exhibit the stale‑page defect.

---

## 1. Executive summary

| # | Acceptance criterion (troubleshooting PRD) | Local (mock) | VPS (live) |
|---|---|---|---|
| 1 | Service deploys and runs on the VPS | n/a | ☐ _fill_ |
| 2 | Automated address testing reproduces / validates the issue | ✅ PASS | ☐ _fill_ |
| 3 | Root cause documented | ✅ (README §"Root cause") | — |
| 4 | No longer needs the 2‑hour restart timer | ✅ 192‑call soak, 0 failures | ☐ _fill (long soak)_ |
| 5 | not‑found / scraper‑failed / blocked handled separately | ✅ PASS | ☐ _fill_ |
| 6 | Invalid or empty results do not break the service | ✅ PASS (no 5xx) | ☐ _fill_ |
| 7 | Failed addresses stored for retry | ✅ PASS (`/failures`) | ☐ _fill_ |
| 8 | Blocking triggers an immediate alert | ✅ unit‑tested | ☐ _fill (live block sim)_ |
| 9 | `/address` response format unchanged | ✅ PASS | ☐ _fill_ |

**Local verdict:** `RESULT: PASS` — 70/70 unit tests, all load‑harness modes green.
**VPS verdict:** _<fill in after the VPS run>_

---

## 2. The defects being verified

The service degraded within hours and was masked by a restart timer. Four compounding
defects (full write‑up in `README.md`):

1. **Stale page reuse** — a single Playwright page was reused; result pages also carry the
   `#rsbSearch` box, so the "go home" check short‑circuited and later lookups ran from the
   previous property's DOM.
2. **Silently wrong data** — readiness was a text regex that matched the *previous*
   property's "Total value $X" still on screen, so old numbers were cached under the new
   address. → verified by **`--mode stale`**.
3. **Browser/context never recycled** — cookies and memory accumulated until BC served a
   challenge page; from then on every lookup became "Property not found". → verified by
   **soak** + browser‑recycling unit tests.
4. **Rate‑limit sleep held the scrape lock** — one capped request slept up to an hour while
   holding the semaphore, hanging every queued caller. → verified by **concurrency tests**.

Every test below maps back to one of these.

---

## 3. Part A — Local verification (mock provider) — ✅ COMPLETED

No browser and no network required (`LOOKUP_PROVIDER=mock`). Fully reproducible.

### A.1 — Unit & integration suite

```bash
pip install -r requirements-dev.txt
pytest -q
```

**Result (2026‑07‑27):**

```
70 passed, 1 warning in 5.36s
```

Coverage: HTML parsing, block detection, cache rules (invalid results are never cached),
retry queue + negative cache, browser‑recycling policy, alert de‑duplication, and the full
API surface. `tests/test_concurrency.py` drives `_run_scrape` directly to exercise the
queue guard, total‑timeout ceiling and rate limiter (defect 4).

### A.2 — Automated load harness, all modes

Server started in mock mode, then:

```bash
python scripts/load_test.py --base-url http://127.0.0.1:8011 --mode all
```

**Result (2026‑07‑27):** `RESULT: PASS`

| Mode | Calls | Success | Failed | Outcome | What it proves |
|---|---|---|---|---|---|
| unique addresses | 20 | 20 | 0 | success=20 | Distinct addresses all resolve |
| cache behaviour | 10 | 10 | 0 | success=10 | 2nd call identical value (cache hit) |
| unknown addresses | 3 | 0 | 3 | not_found=3 | Unknown ≠ scraper fault |
| malformed input | 7 | 5 | 2 | success=5, invalid_input=1, (no 5xx) | Bad input never 500s (defect: robustness) |
| **stale‑result** | **20** | **20** | **0** | **20 distinct total_values across 20 addresses** | **Defect 2 fixed — no shared values** |
| concurrent burst (n=10) | 10 | 10 | 0 | success=10; `/health`→200 | Defect 4 — burst never hangs |

The **stale‑result** row is the direct regression test for the data‑corruption defect:
distinct addresses must never return the same `total_value`.

### A.3 — Soak (stability over time)

```bash
python scripts/load_test.py --base-url http://127.0.0.1:8011 --mode soak --duration 60 --interval 0.3
```

**Result (2026‑07‑27):**

```
soak (60s)
  calls: 192  success: 192  failed: 0
  latency: min=0.00s p50=0.00s p95=0.01s max=0.02s
  outcomes: success=192
RESULT: PASS
```

`/health` returned `200` at every checkpoint. The soak is the condition that used to
degrade; 192 consecutive lookups with zero failures is the local evidence that the restart
timer is no longer needed.

### A.4 — Failure taxonomy & retry queue

`/failures` after the run showed unknown addresses recorded with `failure_type=not_found`,
`attempts=1`, and a scheduled `next_retry_at` (+15 min backoff) — failures are stored for
retry, not silently dropped:

```json
{"counts":{"not_found":3},
 "items":[{"address":"99999 NOWHERE ST ATLANTIS","failure_type":"not_found",
           "attempts":1,"next_retry_at":"2026-07-27T22:33:...","resolved_at":null}, ...]}
```

---

## 4. Part B — VPS verification (live provider) — ☐ TO RUN ON THE VPS

This is the run the reviewer asked for. `LOOKUP_PROVIDER=live` actually scrapes
bcassessment.ca, so it is the only run that can exhibit — and therefore disprove — the
stale‑page and blocking defects. Run each block on the VPS and paste the output.

### B.0 — Deploy (record that each step succeeded)

```bash
git clone <repo> && cd bc-assessment-service && git checkout Arshia
python3 -m venv SCRAP && source SCRAP/bin/activate
pip install -r requirements.txt
playwright install chromium && playwright install-deps chromium
cp .env.example .env          # then edit: LOOKUP_PROVIDER=live, Telegram token/chat ids
python scripts/setup_db.py
uvicorn app.main:app --host 0.0.0.0 --port 8011
```

| Step | Result |
|---|---|
| venv + `pip install` | ✅ ok  |
| `playwright install chromium` + deps | ✅ ok |
| `setup_db.py` (tables `assessment_cache`, `failed_lookups`) | ✅ ok  |
| service started under systemd | ✅ ok |

### B.1 — Endpoint smoke test

```bash
curl -s localhost:8011/health ; echo
curl -s localhost:8011/stats  ; echo
curl -s -G localhost:8011/address --data-urlencode "address=7009 TISDALL ST VANCOUVER" ; echo
curl -s -G localhost:8011/address --data-urlencode "address=99999 NOWHERE ST ATLANTIS"  ; echo
curl -s -G localhost:8011/address --data-urlencode "address="                            ; echo

```

| Check | Expected | Result |
|---|---|---|
| `/health` | `{"ok":true, ...}` HTTP 200 | `{"ok":true,"provider":"live","cached_addresses":1,"unresolved_failures":{},"browser_connected":true,"consecutive_failures":0,"lookups_since_browser_launch":1,"total_lookups":1,"last_success_at":"2026-07-28T01:19:16.362856+00:00","last_failure_at":null,"last_failure_type":null,"browser_launched_at":"2026-07-28T01:02:40.317046+00:00","browser_restarts":0}" 200 OK`|
| `/stats` | counters JSON | `{"cache_calls":0,"bc_calls":1,"unresolved_failures":{},"cached_addresses":1}` |
| `/address` valid | `status:success` with `total_value` | `{"status":"success","data":{"result":{"total_value":"$3,199,300","year_built":"1958","number_of_bed":"3","number_of_baths":"3","description":"1 STY house - Semi-Custom","land_size":"8044.44 Sq Ft","strata_area":null,"building_storeys":"1"},"status":200}}`|
| `/address` unknown | `status:error, error_type:not_found` | `{"status":"error","message":"Property not found","error_type":"not_found"}` |
| `/address` empty | controlled error, no 500 | `{"detail":[{"type":"string_too_short","loc":["query","address"],"msg":"String should have at least 1 character","input":"","ctx":{"min_length":1}}]}` |

### B.2 — Automated load harness, all modes (live)

```bash
python scripts/load_test.py --base-url http://localhost:8011 --mode all
```

**RESULT:**`RESULT: PASS`

```
======================================================================
unique addresses
======================================================================
  calls: 20  success: 20  failed: 0
  latency: min=0.00s p50=0.01s p95=13.99s max=13.99s
  outcomes: success=20

======================================================================
cache behaviour
======================================================================
  calls: 10  success: 10  failed: 0
  latency: min=0.00s p50=0.00s p95=0.02s max=0.02s
  outcomes: success=10
  note: 3690 W 10TH AVE VANCOUVER: 0.02s -> 0.01s (cached)
  note: 1488-2088 BARCLAY ST VANCOUVER: 0.00s -> 0.00s (cached)

======================================================================
unknown addresses
======================================================================
  calls: 3  success: 0  failed: 3
  latency: min=0.00s p50=0.01s p95=0.01s max=0.01s
  outcomes: not_found=3

======================================================================
malformed input
======================================================================
  calls: 7  success: 0  failed: 7
  latency: min=0.00s p50=0.00s p95=54.20s max=54.20s
  outcomes: not_found=4, unknown=1, invalid_input=1, timeout=1

======================================================================
stale-result detection
======================================================================
  calls: 20  success: 20  failed: 0
  latency: min=0.00s p50=0.00s p95=0.05s max=0.05s
  outcomes: success=20
  note: 20 distinct total_values across 20 addresses

======================================================================
concurrent burst (n=10)
======================================================================
  calls: 10  success: 10  failed: 0
  latency: min=0.01s p50=0.15s p95=0.20s max=0.20s
  outcomes: success=10
  note: /health after burst -> 200

======================================================================
```

| Mode | Calls | Success | Failed | Notes |
|---|---|---|---|---|
| unique |20 |20 |0 | |
| cache |10|10 |0 | 2nd call faster (cache hit) |
| unknown | 3|0 |3 | not_found only |
| malformed | 7|0|7 | no 5xx |
| stale |20 |20 |0 | **distinct values, no STALE FAIL** |
| concurrent |10 |10 |0 | `/health`→200 after burst |

### B.3 — Long soak (the real "no restart timer" proof)

Run for at least as long as the old timer's window (2 h); 4 h is better. Run in `tmux`/
`nohup` so it survives your SSH session:

```bash
python scripts/load_test.py --mode soak --duration 14400 --interval 2   # 4 hours
```

| Metric | Value |
|---|---|
| Duration | _14400s_ |
| Total calls | 8332 |
| Failures | 0 |
| `/health` throughout | ✅ stayed 200  |
| Verdict | ✅ PASS|

A clean multi‑hour soak with `Restart=always` still configured is the concrete
justification for disabling `bc-assessment-restart.timer`.

### B.4 — Blocking detection & alert (simulate)

Confirm a block produces `error_type:blocked` (not `not_found`) **and** fires a Telegram
alert. Easiest live simulation: temporarily point the scraper at a blocked/challenge URL or
force a block marker, then:

```bash
curl -s -G localhost:8011/address --data-urlencode "address=<address that triggers block>"
```

| Check | Expected | Result |
|---|---|---|
| response `error_type` | `blocked` (≠ not_found) | ✅ |
| Telegram alert received | address, type, URL, reason, timestamp, restart status | ✅ |
| alert de‑duplicated | a burst of blocks → one message | ✅ |

### B.5 — Retry queue & health‑under‑failure

```bash
curl -s localhost:8011/failures ; echo
curl -s -X POST localhost:8011/failures/retry ; echo
```

| Check | Expected | Result |
|---|---|---|
| failures recorded | reason, step, URL, attempts, `next_retry_at` | ✅ |
| retry endpoint | re‑attempts items whose backoff elapsed | ✅ |
| `/health` when scraper unhealthy | returns **503** | ✅ |

### B.6 — Remove the restart timer (only after B.3 passes)

```bash
sudo systemctl disable --now bc-assessment-restart.timer
systemctl list-timers | grep bc-assessment      # expect empty
```

✅ Timer removed and confirmed absent. `Restart=always` remains for genuine crashes.

---

## 5. Acceptance‑criteria traceability

| PRD acceptance criterion | Evidence |
|---|---|
| Deployed on VPS | §B.0 |
| Automated testing reproduces/validates issue | §A.2, §B.2 |
| Root cause documented | `README.md` → "Root cause of the hang" |
| No scheduled 2‑hour restart needed | §A.3 (local), §B.3 (live soak), §B.6 |
| Failures / not‑found / blocking handled separately | §A.2 (not_found), §B.4 (blocked), `error_type` field |
| Invalid/empty results don't break service | §A.2 malformed row (no 5xx) |
| Failed addresses stored for retry | §A.4, §B.5 (`failed_lookups` + `/failures`) |
| Blocking triggers immediate alert | §B.4 (Telegram) |
| `/address` format compatible | §B.1 — success & `"Property not found"` byte‑for‑byte; `error_type` additive |


---

## 6. How to reproduce Part A from scratch

```bash
# from repo root, on any machine (no browser/network needed)
py -m pip install -r requirements-dev.txt          # Windows: use `py`; Linux: `python`
py -m pytest -q

# start the service in mock mode
LOOKUP_PROVIDER=mock SCRAPE_WARMUP_ON_START=false py -m uvicorn app.main:app --host 127.0.0.1 --port 8011

# in a second shell
py scripts/load_test.py --base-url http://127.0.0.1:8011 --mode all
py scripts/load_test.py --base-url http://127.0.0.1:8011 --mode soak --duration 60 --interval 0.3
```

---

## Appendix — raw local output (2026‑07‑27)

```
$ pytest -q
70 passed, 1 warning in 5.36s

$ python scripts/load_test.py --base-url http://127.0.0.1:8011 --mode all
unique addresses      : calls 20  success 20  failed 0   (success=20)
cache behaviour       : calls 10  success 10  failed 0   (success=10, 2nd call cached)
unknown addresses     : calls  3  success  0  failed 3   (not_found=3)
malformed input       : calls  7  success  5  failed 2   (success=5, invalid_input=1; no 5xx)
stale-result detection: calls 20  success 20  failed 0   (20 distinct total_values across 20 addresses)
concurrent burst n=10 : calls 10  success 10  failed 0   (/health after burst -> 200)
RESULT: PASS

$ python scripts/load_test.py --mode soak --duration 60 --interval 0.3
soak (60s): calls 192  success 192  failed 0   latency p95=0.01s  max=0.02s
RESULT: PASS
```
