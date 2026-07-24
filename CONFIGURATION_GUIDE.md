# BC Assessment Service — Ubuntu Configuration & Deployment Guide

A complete, copy-paste, step-by-step guide for deploying this FastAPI service on a fresh
Ubuntu server, running it under systemd behind nginx (optionally with HTTPS), connecting it
to another app, and troubleshooting every failure mode you are likely to hit.

This guide assumes **Ubuntu 22.04 or 24.04 LTS**. Differences between the two are called out
where they matter (mainly a Playwright dependency package rename).

> **Terminology.** The virtualenv is conventionally named `SCRAP/` (not `.venv/`), because the
> systemd unit, README, and this guide all reference that exact path. If you rename it, update
> every `SCRAP/bin/...` path below.

---

## Table of contents

1. [What you are deploying](#1-what-you-are-deploying)
2. [Before you start (prerequisites & assumptions)](#2-before-you-start)
3. [Step 1 — System packages](#step-1--system-packages)
4. [Step 2 — Create a service user (recommended)](#step-2--create-a-service-user-recommended)
5. [Step 3 — Get the code](#step-3--get-the-code)
6. [Step 4 — Python virtual environment](#step-4--python-virtual-environment)
7. [Step 5 — Install Python dependencies](#step-5--install-python-dependencies)
8. [Step 6 — Install Playwright Chromium + browser libraries](#step-6--install-playwright-chromium--browser-libraries)
9. [Step 7 — Configure `.env`](#step-7--configure-env)
10. [Step 8 — Initialize the database](#step-8--initialize-the-database)
11. [Step 9 — First run & smoke test (manual)](#step-9--first-run--smoke-test-manual)
12. [Step 10 — Run under systemd](#step-10--run-under-systemd)
13. [Step 11 — nginx reverse proxy](#step-11--nginx-reverse-proxy)
14. [Step 12 — HTTPS with Let's Encrypt](#step-12--https-with-lets-encrypt)
15. [Step 13 — Firewall (ufw)](#step-13--firewall-ufw)
16. [Step 14 — Telegram alerts (optional but recommended)](#step-14--telegram-alerts-optional-but-recommended)
17. [Step 15 — Remove the legacy 2-hour restart timer](#step-15--remove-the-legacy-2-hour-restart-timer)
18. [Connecting this service to another app](#connecting-this-service-to-another-app)
19. [Day-2 operations (logs, restart, health, retries)](#day-2-operations)
20. [Environment variable reference](#environment-variable-reference)
21. [Troubleshooting](#troubleshooting)
22. [Verification checklist](#verification-checklist)

---

## 1. What you are deploying

A FastAPI service that returns BC Assessment property data for a formatted address:

```
GET /address?address=7009 TISDALL ST VANCOUVER
```

It checks a SQLite cache first; on a miss it scrapes `bcassessment.ca` with Playwright
(Chromium) and parses the page with BeautifulSoup. Successful results are cached; failures go
to a separate retry queue and never pollute the cache.

**Endpoints the app exposes (on `127.0.0.1:8011` by default):**

| Endpoint | Purpose |
|---|---|
| `GET /address?address=...` | The main lookup. Returns the property JSON or a classified error. |
| `GET /health` | Real scraper health. Returns **503** when the browser is unhealthy. |
| `GET /stats` | Request counters, unresolved failures, cache size. |
| `GET /failures` | Inspect the retry queue. |
| `POST /failures/retry` | Re-attempt failures whose backoff has elapsed. |

**Success response shape (this is a contract — do not change it):**

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

**Error response shape** (the `error_type` field is additive; the `message` for a genuine miss
stays exactly `"Property not found"`):

```json
{ "status": "error", "message": "Property not found",            "error_type": "not_found" }
{ "status": "error", "message": "Lookup temporarily unavailable", "error_type": "blocked" }
{ "status": "error", "message": "Lookup timed out",               "error_type": "timeout" }
```

---

## 2. Before you start

**You need:**

- A Ubuntu 22.04 or 24.04 LTS server (a 1 GB / 1 vCPU VPS works, but **2 GB RAM is strongly
  recommended** — Chromium is memory-hungry and can be OOM-killed on 1 GB under load).
- SSH access with a user that has `sudo`.
- (For a public domain + HTTPS) A domain name with an **A record** pointing at the server's
  public IP.
- Outbound internet access from the server (to reach `bcassessment.ca` and, during install,
  PyPI + the Playwright browser CDN).

**Decide up front:**

- **Which host path?** The app itself serves at `/address`. To expose it publicly as
  `https://yourdomain.com/bc-assessment/address`, nginx strips the `/bc-assessment/` prefix
  (Step 11). The app has no built-in base-path setting, so **do not** expect the app to answer
  on `/bc-assessment/...` directly — that stripping happens in nginx.
- **Provider mode.** Production uses `LOOKUP_PROVIDER=live` (real scraping). Use `mock` only
  for smoke-testing the plumbing without a browser or network.

All commands below assume you run them from the project directory unless noted. Paths in the
systemd unit assume the project lives at `/root/bc-assessment-service`. If you use a
non-root service user (recommended, Step 2), substitute that home directory everywhere.

---

## Step 1 — System packages

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git curl
```

Confirm Python is 3.10+ (the code uses 3.10+ syntax; 3.11/3.12 on 22.04/24.04 are fine):

```bash
python3 --version
```

Install nginx now if you plan to use the reverse proxy (Step 11):

```bash
sudo apt install -y nginx
```

---

## Step 2 — Create a service user (recommended)

Running the scraper as `root` works and matches the README's example unit, but a dedicated
unprivileged user is safer. **If you prefer to just use `root`, skip this step** and read
`/root/bc-assessment-service` wherever this guide says `/home/bcassess/bc-assessment-service`.

```bash
sudo adduser --system --group --home /home/bcassess --shell /bin/bash bcassess
sudo mkdir -p /home/bcassess
sudo chown -R bcassess:bcassess /home/bcassess
```

To run the following steps as that user:

```bash
sudo -u bcassess -H bash
cd /home/bcassess
```

> **Important:** Playwright downloads its browser into the running user's home
> (`~/.cache/ms-playwright`). Whatever user you install the browser as **must be the same user
> the systemd service runs as**, or the service will not find Chromium. This is the single most
> common deployment mistake — see [Troubleshooting](#browser-not-found--executable-doesnt-exist).

---

## Step 3 — Get the code

```bash
git clone <YOUR_REPO_URL> bc-assessment-service
cd bc-assessment-service
```

If the repo is private, either use an SSH deploy key or clone over HTTPS with a personal
access token. From here on, this directory is your **project root**; commands assume you are in
it.

---

## Step 4 — Python virtual environment

```bash
python3 -m venv SCRAP
source SCRAP/bin/activate
```

Your prompt should now start with `(SCRAP)`. Upgrade pip inside the venv:

```bash
pip install --upgrade pip
```

> If `python3 -m venv` fails with `ensurepip is not available`, install the venv package for
> your Python version, e.g. `sudo apt install -y python3-venv` (or `python3.12-venv` on 24.04).

---

## Step 5 — Install Python dependencies

```bash
pip install -r requirements.txt
```

This installs FastAPI, uvicorn, pydantic, BeautifulSoup4, lxml, Playwright, and httpx.

---

## Step 6 — Install Playwright Chromium + browser libraries

Two parts: the browser binary, then the OS libraries Chromium links against.

```bash
playwright install chromium
playwright install-deps chromium
```

`install-deps` needs `sudo` privileges (it runs `apt`). If you are inside a `sudo -u bcassess`
shell **without** sudo rights, run the deps step separately as an admin user:

```bash
# From a user with sudo (browser binary already installed by bcassess above):
sudo /home/bcassess/bc-assessment-service/SCRAP/bin/playwright install-deps chromium
```

**If `install-deps` fails** (common on minimal images, or if a package name changed), install
the libraries manually.

- **Ubuntu 22.04:**

  ```bash
  sudo apt install -y libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libgbm1 libasound2
  ```

- **Ubuntu 24.04** (`libasound2` was renamed to `libasound2t64`):

  ```bash
  sudo apt install -y libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libgbm1 libasound2t64
  ```

If you are unsure which is available:

```bash
apt-cache search libasound2
```

Verify Chromium actually launches (this catches missing-library problems before you deploy):

```bash
python -c "from playwright.sync_api import sync_playwright; \
p=sync_playwright().start(); b=p.chromium.launch(); print('chromium OK'); b.close(); p.stop()"
```

If this prints `chromium OK`, the browser is installed correctly. Any error here is a missing
system library — read it and install the named `.so`'s package.

---

## Step 7 — Configure `.env`

```bash
cp .env.example .env
nano .env
```

`.env` is **gitignored — never commit it.** The defaults are production-sane. The values you
are most likely to touch:

| Variable | Recommended | Why |
|---|---|---|
| `LOOKUP_PROVIDER` | `live` | Real scraping. Use `mock` only for smoke tests. |
| `SCRAPE_CACHE_TTL_SECONDS` | `0` | `0` = never expire. BC Assessment updates ~yearly; a low TTL re-scrapes constantly and gets you throttled. **Leave at 0.** |
| `TELEGRAM_BOT_TOKEN` | _(your token)_ | Required for block alerts. See Step 14. |
| `TELEGRAM_CHAT_IDS` | _(your chat id)_ | Comma-separated. |
| `PORT` | `8011` | Must match the systemd unit and nginx `proxy_pass`. |

The full list is in the [Environment variable reference](#environment-variable-reference).

> **Security check:** the Telegram token must live in `.env` only. There is a regression test
> asserting the defaults ship empty. Do not paste a real token into any committed file.

---

## Step 8 — Initialize the database

```bash
python scripts/setup_db.py
```

This creates `data/bc_assessment.db` with the `assessment_cache` and `failed_lookups` tables.
It is **idempotent and doubles as the migration** — safe to re-run on an existing database; it
adds `failed_lookups` without touching cached results. Expected output:

```
Cache database created: /.../data/bc_assessment.db
  tables: assessment_cache, failed_lookups
  cached addresses: 0
  unresolved failures: 0
```

The `data/` directory is created automatically. Make sure the service user owns it:

```bash
chown -R bcassess:bcassess data/    # skip if running as root
```

---

## Step 9 — First run & smoke test (manual)

Before wiring up systemd, confirm the app runs in the foreground. Two options:

```bash
python -m app.main
# or, closer to the production invocation:
uvicorn app.main:app --host 0.0.0.0 --port 8011
```

In **another SSH session**, hit the endpoints:

```bash
curl -s localhost:8011/health
curl -s "localhost:8011/address?address=mock"   # only meaningful in mock mode
curl -s localhost:8011/stats
```

A real lookup (live provider):

```bash
curl -sG "localhost:8011/address" --data-urlencode "address=7009 TISDALL ST VANCOUVER"
```

The **first live lookup takes several seconds** (browser warmup + scrape). A repeat of the same
address should return instantly from cache. When it works, stop the foreground process with
`Ctrl+C` and move to systemd.

> **Fast plumbing check without a browser:** set `LOOKUP_PROVIDER=mock` in `.env`, restart, and
> the magic address tokens work: any address returns deterministic mock data; addresses
> containing `NOWHERE`/`INVALID`/`DOESNOTEXIST` return `not_found`; `BLOCKED` returns `blocked`.
> Set it back to `live` before going to production.

---

## Step 10 — Run under systemd

Create the unit. **Adjust `User`, `WorkingDirectory`, and the two paths** if you are not running
as root at `/root/bc-assessment-service`.

```bash
sudo nano /etc/systemd/system/bc-assessment.service
```

For a **root** install (matches the README):

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

For a **dedicated user** install (Step 2), use these instead:

```ini
[Service]
Type=simple
User=bcassess
Group=bcassess
WorkingDirectory=/home/bcassess/bc-assessment-service
Environment=PATH=/home/bcassess/bc-assessment-service/SCRAP/bin
Environment=HOME=/home/bcassess
ExecStart=/home/bcassess/bc-assessment-service/SCRAP/bin/uvicorn app.main:app --host 0.0.0.0 --port 8011
Restart=always
RestartSec=5
```

> The `Environment=HOME=...` line matters: Playwright looks in `$HOME/.cache/ms-playwright` for
> Chromium. Without it systemd may pass a different `HOME` and the browser won't be found.

Enable and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bc-assessment
sudo systemctl status bc-assessment
```

`status` should show `active (running)`. Confirm it survives a reboot later with
`sudo reboot` and re-checking `systemctl status`.

Bind the app to `127.0.0.1` instead of `0.0.0.0` if you will **only** reach it through nginx and
want it off the public internet — change `--host 0.0.0.0` to `--host 127.0.0.1`. (The README's
example binds `0.0.0.0`; either works behind nginx, but `127.0.0.1` is safer.)

---

## Step 11 — nginx reverse proxy

Goal: expose the app publicly at `https://yourdomain.com/bc-assessment/...` while the app keeps
serving at `/address` internally. nginx strips the `/bc-assessment/` prefix via a trailing-slash
`proxy_pass`.

```bash
sudo nano /etc/nginx/sites-available/bc-assessment
```

```nginx
server {
    listen 80;
    server_name yourdomain.com;   # or the server's public IP for IP-only access

    # Everything under /bc-assessment/ is forwarded to the app with the prefix stripped.
    # Trailing slashes on BOTH location and proxy_pass are what strip the prefix:
    #   /bc-assessment/address?address=X  ->  http://127.0.0.1:8011/address?address=X
    location /bc-assessment/ {
        proxy_pass http://127.0.0.1:8011/;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # A live scrape can take up to a minute; give the proxy room so it does not
        # 504 before the app's own SCRAPE_TOTAL_TIMEOUT_MS (default 60s) fires.
        proxy_read_timeout    120s;
        proxy_connect_timeout 10s;
    }
}
```

Query strings are preserved automatically by `proxy_pass`. Enable the site and reload:

```bash
sudo ln -s /etc/nginx/sites-available/bc-assessment /etc/nginx/sites-enabled/
sudo nginx -t          # test config; must say "syntax is ok" / "test is successful"
sudo systemctl reload nginx
```

Verify through nginx:

```bash
curl -s http://yourdomain.com/bc-assessment/health
curl -sG "http://yourdomain.com/bc-assessment/address" \
  --data-urlencode "address=7009 TISDALL ST VANCOUVER"
```

> **Note on the trailing slash.** `location /bc-assessment/` (with slash) + `proxy_pass
> http://127.0.0.1:8011/` (with slash) is what removes the prefix. If you drop either slash the
> app will receive `/bc-assessment/address` and return 404. Keep both.

> If you want the API on the **domain root** (`https://yourdomain.com/address`) with no prefix,
> use `location /` and `proxy_pass http://127.0.0.1:8011;` (no trailing slash on either).

---

## Step 12 — HTTPS with Let's Encrypt

Requires a real domain with DNS pointing at the server, and port 80 reachable.

```bash
sudo apt install -y certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
```

Certbot edits the nginx config to add the `443` server block and an HTTP→HTTPS redirect, then
reloads nginx. Confirm auto-renewal is scheduled:

```bash
sudo certbot renew --dry-run
systemctl list-timers | grep certbot
```

Re-test over HTTPS:

```bash
curl -s https://yourdomain.com/bc-assessment/health
```

**If you cannot use HTTPS** (no domain, IP only): that is acceptable per the PRD as long as you
document it. The API still works over HTTP through nginx; just don't send anything sensitive.

---

## Step 13 — Firewall (ufw)

If the server uses `ufw`, allow SSH and web traffic before enabling it (or you will lock
yourself out):

```bash
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'    # opens 80 + 443
sudo ufw enable
sudo ufw status
```

Do **not** publicly expose port `8011`. Traffic should reach the app only through nginx on
80/443. If the app binds `0.0.0.0:8011`, keep 8011 closed in ufw (it is, unless you explicitly
allow it).

---

## Step 14 — Telegram alerts (optional but recommended)

The service sends an immediate alert when it detects blocking/throttling/challenge pages, with
address, failure type, URL, reason, timestamp, and whether the browser was restarted. Alerts are
de-duplicated (a block storm sends one message) and an all-clear is sent on recovery.

1. In Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`, follow the prompts,
   and copy the **bot token**.
2. Get your **chat id**: message your new bot once, then open
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` in a browser and read
   `result[].message.chat.id`. (For a group, add the bot to the group and read the negative
   group chat id the same way.)
3. Put both in `.env`:

   ```ini
   TELEGRAM_BOT_TOKEN=123456789:AA...your-token...
   TELEGRAM_CHAT_IDS=987654321
   ```

   Multiple recipients are comma-separated: `TELEGRAM_CHAT_IDS=111,222`.
4. Restart the service: `sudo systemctl restart bc-assessment`.

Alerts stay silent until both values are set. Verify by watching logs while forcing a failure,
or trust the recovery/all-clear message on the next block event.

---

## Step 15 — Remove the legacy 2-hour restart timer

If this server previously ran the old workaround that restarted the service every 2 hours, it
is no longer the fix and should be removed **after** the service has soaked successfully:

```bash
sudo systemctl disable --now bc-assessment-restart.timer
systemctl list-timers | grep bc-assessment    # should print nothing
```

`Restart=always` in the unit remains, so the service still auto-recovers from a genuine crash.
If no such timer exists on your box, skip this step.

---

## Connecting this service to another app

The consumer calls the `/address` endpoint over HTTP and reads the JSON. Point it at whichever
URL you exposed:

| Deployment | Base URL to give the consumer |
|---|---|
| Direct (same host / private network) | `http://SERVER_IP:8011/address` |
| Behind nginx, prefixed | `https://yourdomain.com/bc-assessment/address` |
| Behind nginx, root path | `https://yourdomain.com/address` |

**Example — BlueNest's `BCAssessmentClient`** reads two env vars on its side:

```bash
BC_ASSESSMENT_API_URL=https://yourdomain.com/bc-assessment/address
BC_ASSESSMENT_TIMEOUT=30
```

**Example — any generic client** (curl, and the equivalent request from code):

```bash
curl -sG "https://yourdomain.com/bc-assessment/address" \
  --data-urlencode "address=7009 TISDALL ST VANCOUVER"
```

```python
import httpx

resp = httpx.get(
    "https://yourdomain.com/bc-assessment/address",
    params={"address": "7009 TISDALL ST VANCOUVER"},
    timeout=30,
)
data = resp.json()
if data["status"] == "success":
    result = data["data"]["result"]           # total_value, year_built, ...
else:
    # data["message"] is human-readable; data["error_type"] distinguishes the cause:
    #   not_found | blocked | timeout | parse_empty | error | busy | invalid_input
    kind = data.get("error_type")
```

**Contract notes the caller must respect:**

- A genuine miss returns `message: "Property not found"` — that exact string is stable. Match on
  `error_type == "not_found"` rather than the message if you can.
- `error_type == "blocked"` or `"timeout"` means *transient* — the caller should retry later, not
  treat the address as nonexistent.
- `error_type == "busy"` means the service shed load under a queue backlog — retry shortly.
- Set the client timeout to **at least 30 s**; a first-time live lookup includes browser work.
- Cache-hit responses are near-instant; only uncached addresses incur the scrape cost and the
  rate-limit spacing (`BC_RATE_LIMIT_MIN_INTERVAL_SECONDS`, default 4 s between BC visits).

---

## Day-2 operations

```bash
# Follow logs live (address, failure type, step, URL, and reason are logged per failure)
sudo journalctl -u bc-assessment -f

# Last 200 log lines
sudo journalctl -u bc-assessment -n 200 --no-pager

# Restart / stop / start
sudo systemctl restart bc-assessment
sudo systemctl stop bc-assessment
sudo systemctl start bc-assessment

# Scraper health (HTTP 503 body = unhealthy browser)
curl -s localhost:8011/health

# Counters, unresolved failures, cache size
curl -s localhost:8011/stats

# Inspect the retry queue
curl -s "localhost:8011/failures"
curl -s "localhost:8011/failures?due_only=true"
curl -s "localhost:8011/failures?include_resolved=true"

# Re-attempt failures whose backoff has elapsed
curl -s -X POST "localhost:8011/failures/retry"
```

**Updating the code:**

```bash
cd /home/bcassess/bc-assessment-service      # or /root/bc-assessment-service
git pull
source SCRAP/bin/activate
pip install -r requirements.txt              # if requirements changed
python scripts/setup_db.py                   # safe/idempotent; applies any migration
sudo systemctl restart bc-assessment
```

**Backups:** the only stateful file is the SQLite DB at `data/bc_assessment.db`. Copy it (plus
the `-wal`/`-shm` sidecars if present) while the service is stopped, or use `sqlite3 ... ".backup"`
for a hot copy.

---

## Environment variable reference

| Variable | Default | Purpose |
|---|---|---|
| `HOST` / `PORT` | `0.0.0.0` / `8011` | Bind address. `PORT` must match nginx `proxy_pass` and the systemd `ExecStart`. |
| `LOOKUP_PROVIDER` | `live` | `live` scrapes BC; `mock` serves fixtures (tests / smoke). |
| `DATABASE_PATH` | `data/bc_assessment.db` | SQLite file (relative to project root). |
| `SCRAPE_CACHE_TTL_SECONDS` | `0` | Cache lifetime; `0` = never expire (**recommended**). |
| `NOT_FOUND_CACHE_TTL_SECONDS` | `86400` | How long a genuine not-found is remembered. |
| `BC_RATE_LIMIT_MAX` | `15` | Max scrapes per short window. |
| `BC_RATE_LIMIT_WINDOW_SECONDS` | `60` | Short rate-limit window. |
| `BC_RATE_LIMIT_HOURLY_MAX` | `500` | Hourly cap on BC visits. |
| `BC_RATE_LIMIT_MIN_INTERVAL_SECONDS` | `4` | Minimum spacing between BC requests. |
| `SCRAPE_WARMUP_ON_START` | `true` | Pre-launch the browser at boot. |
| `SCRAPE_AUTOCOMPLETE_TIMEOUT_MS` | `8000` | Wait for autocomplete suggestions. |
| `SCRAPE_RESULT_TIMEOUT_MS` | `25000` | Wait for the property page. |
| `SCRAPE_TOTAL_TIMEOUT_MS` | `60000` | Hard ceiling on one whole lookup. |
| `SCRAPE_MAX_QUEUE_DEPTH` | `20` | Queued requests before shedding load (`busy`). |
| `BROWSER_MAX_LOOKUPS` | `200` | Recycle the browser after N lookups (`0` = never). |
| `BROWSER_RESTART_AFTER_FAILURES` | `3` | Recycle after N consecutive failures. |
| `UNHEALTHY_AFTER_FAILURES` | `5` | Failures before `/health` returns 503. |
| `SAVE_HTML_SNAPSHOTS` | `true` | Save HTML for blocked/unexpected pages. |
| `SNAPSHOT_DIR` / `SNAPSHOT_KEEP` | `data/snapshots` / `50` | Snapshot location and retention. |
| `TELEGRAM_BOT_TOKEN` | _(empty)_ | From @BotFather. Set in `.env` only. |
| `TELEGRAM_CHAT_IDS` | _(empty)_ | Comma-separated chat IDs. |
| `STATS_MILESTONE_COUNT` | `100` | Reset counters and alert at this many calls. |
| `ALERT_MIN_INTERVAL_SECONDS` | `300` | Minimum gap between repeat alerts of one type. |

---

## Troubleshooting

Work top-to-bottom; the earlier items are the more common causes.

### The service won't start (`systemctl status` shows `failed`)

```bash
sudo journalctl -u bc-assessment -n 50 --no-pager
```

Read the traceback. The usual culprits:

- **`ExecStart` path wrong** — the venv or project path in the unit doesn't exist. Confirm
  `/…/SCRAP/bin/uvicorn` actually exists: `ls -l /home/bcassess/bc-assessment-service/SCRAP/bin/uvicorn`.
- **Wrong `WorkingDirectory`** — the app can't find `app/` or `.env`. It must be the project root.
- **Permissions** — the `User` in the unit can't read the project or write `data/`. Fix with
  `sudo chown -R bcassess:bcassess /home/bcassess/bc-assessment-service`.
- **Edited the unit but didn't reload** — run `sudo systemctl daemon-reload` after any change to
  the `.service` file, then `sudo systemctl restart bc-assessment`.

### Browser not found / "Executable doesn't exist"

Log shows something like `Executable doesn't exist at /root/.cache/ms-playwright/chromium-*/...`.

**Cause:** Playwright's browser was installed as a *different user* than the one the service runs
as. The browser lives in `$HOME/.cache/ms-playwright`.

**Fix:** install the browser as the exact service user, and make sure systemd passes that user's
`HOME`:

```bash
sudo -u bcassess -H bash -lc 'cd /home/bcassess/bc-assessment-service && source SCRAP/bin/activate && playwright install chromium'
```

and ensure the unit has `Environment=HOME=/home/bcassess` (Step 10). Then
`sudo systemctl restart bc-assessment`.

### "Host system is missing dependencies" / Chromium fails to launch

A required shared library is missing. Re-run `playwright install-deps chromium`, or install the
libs manually — remember **Ubuntu 24.04 uses `libasound2t64`**, not `libasound2` (Step 6). The
one-liner launch test in Step 6 will name the exact missing `.so`.

### Every lookup returns "Property not found" and `/health` is 503

This is the classic block/wedge symptom. Check:

```bash
curl -s localhost:8011/health          # look at consecutive_failures / ok:false
curl -s localhost:8011/failures        # what failure_type dominates?
sudo journalctl -u bc-assessment -n 100 --no-pager
```

- If failures are `blocked` — BC Assessment is challenging the scraper. Check
  `data/snapshots/` for the saved HTML of the blocked page. Back off (the rate limiter should
  already be doing this), wait, and let the browser recycle. A Telegram alert should have fired
  if configured.
- If failures are `timeout` — the site is slow or the network is constrained. Confirm the server
  can reach BC at all: `curl -sI https://www.bcassessment.ca`.
- If `/health` is 503 but individual lookups now succeed, it will clear itself once
  `UNHEALTHY_AFTER_FAILURES` worth of good lookups pass; or restart the service to force a clean
  browser.

Do **not** re-add a periodic restart timer as "the fix" — the structural fixes (fresh context per
lookup, browser recycling, rate-limit-before-lock, total-timeout ceiling) are what keep it
healthy. A restart is only a manual reset.

### Requests hang / clients time out under load

- A capped-out **hourly** rate-limit wait is not bounded and can block for up to an hour at the
  cap (documented limitation). Callers beyond `SCRAPE_MAX_QUEUE_DEPTH` get a fast `busy` instead
  of hanging — so if clients hang rather than getting `busy`, you are below the queue depth and
  simply waiting on the rate limiter. Lower request volume or raise the caps cautiously
  (`BC_RATE_LIMIT_HOURLY_MAX`) — but raising them risks getting blocked by BC.
- Make sure your nginx `proxy_read_timeout` (Step 11, `120s`) is **longer** than the app's
  `SCRAPE_TOTAL_TIMEOUT_MS` (default 60s), or nginx will 504 before the app can respond.

### 404 through nginx but the app works on `localhost:8011`

The prefix isn't being stripped. Both the `location` and the `proxy_pass` need a trailing slash:
`location /bc-assessment/ { proxy_pass http://127.0.0.1:8011/; }`. Re-test with `sudo nginx -t`
then `sudo systemctl reload nginx`.

### 502 Bad Gateway from nginx

nginx can't reach the app. Confirm the app is up (`sudo systemctl status bc-assessment`), that it
listens on the port nginx targets (`ss -ltnp | grep 8011`), and that the `proxy_pass` port
matches `PORT` in `.env`.

### Query string / address gets mangled

The address contains spaces. When testing with curl, use `-G --data-urlencode "address=..."`
(as shown throughout) so spaces are encoded correctly. Callers should URL-encode the `address`
query param.

### `.env` changes don't take effect

Settings are read from `.env` at process start. After editing `.env`, restart:
`sudo systemctl restart bc-assessment`. (In the manual foreground run, `Ctrl+C` and relaunch.)

### Database is locked / permission denied writing `data/`

The service user can't write the SQLite file or its WAL sidecars. Fix ownership:
`sudo chown -R bcassess:bcassess /home/bcassess/bc-assessment-service/data`. The DB uses WAL mode,
so it tolerates concurrent readers; a persistent "locked" error almost always means a permissions
or path problem, not contention.

### Out-of-memory (service killed periodically, `dmesg` shows OOM)

Chromium under load can exceed a 1 GB VPS. Options: upgrade to 2 GB RAM; lower
`BROWSER_MAX_LOOKUPS` so the browser recycles (frees memory) more often; add swap:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### High disk usage from snapshots

Blocked-page HTML snapshots accumulate in `data/snapshots/`. Retention is capped by
`SNAPSHOT_KEEP` (default 50). Lower it, or set `SAVE_HTML_SNAPSHOTS=false` to disable entirely.

### Telegram alerts never arrive

Confirm both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_IDS` are set in `.env` and the service was
restarted. Test the token by hand:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  -d chat_id=<CHAT_ID> -d text="bc-assessment test"
```

If that fails, the token/chat id is wrong. You must have messaged the bot at least once from that
chat before it can send to you.

### Run the automated tests / load harness

```bash
pip install -r requirements-dev.txt
pytest                                                     # full suite (uses mock provider; no browser)
python scripts/load_test.py --base-url http://localhost:8011 --mode all
python scripts/load_test.py --mode stale                   # stale-result regression check
python scripts/load_test.py --mode soak --duration 14400   # 4-hour soak
```

The `stale` mode is the direct regression test for the old data-corruption bug: it asserts that
distinct addresses never return the same `total_value`.

---

## Verification checklist

Deployment is complete when all of these pass:

- [ ] `sudo systemctl status bc-assessment` shows `active (running)`.
- [ ] `curl -s localhost:8011/health` returns `"ok": true` (HTTP 200, not 503).
- [ ] `curl -s localhost:8011/stats` returns counters.
- [ ] A live `curl -sG localhost:8011/address --data-urlencode "address=7009 TISDALL ST VANCOUVER"`
      returns a `status: success` body with a `total_value`.
- [ ] A repeat of the same address returns instantly (cache hit).
- [ ] An unknown address returns `status: error`, `error_type: not_found`.
- [ ] The public URL works through nginx:
      `curl -s https://yourdomain.com/bc-assessment/health`.
- [ ] HTTPS is configured (or its absence is documented).
- [ ] The service survives a reboot (`sudo reboot`, then re-check `systemctl status`).
- [ ] Telegram alerts are configured (token + chat id in `.env`).
- [ ] The legacy 2-hour restart timer is disabled.
- [ ] The consuming app is pointed at the correct `/address` URL and gets a success response.
```
