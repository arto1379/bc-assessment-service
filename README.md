# BC Assessment API Service
Small FastAPI service that exposes GET /address?address=... in the format expected by BlueNest’s BCAssessmentClient.

What it does
- Listens for: GET /address?address={formatted address}
- Returns JSON like:
```bsh
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
Business logic lives in `lookup_by_address(address)` (`app/services/lookup.py`), which scrapes bcassessment.ca via Playwright.

--

##  Linux prerequisites

Install on the server (Ubuntu/Debian example):

```bsh
sudo apt update
sudo apt install -y python3 python3-pip python3-venv git curl
```

Optional (production):
```bsh
sudo apt install -y nginx   # reverse proxy
# or use Docker instead of bare metal
```

check
```bsh
python3 --version
```

## Project Layout

```bsh

bc-assessment-service/
├── README.md
├── requirements.txt
├── scripts/
│   └── setup_db.py      # one-time SQLite cache setup
├── data/                # SQLite DB (local only, do not commit)
├── SCRAP/               # Python virtualenv (local only, do not commit)
├── .env.example
├── .env                 # local only, do not commit
└── app/
    ├── main.py          # FastAPI routes
    ├── config.py        # host, port, database path
    ├── schemas.py       # response models
    ├── db/
    │   └── cache.py     # SQLite cache read/write
    └── services/
        └── lookup.py    # lookup_by_address() — cache then scrape
```


# Quick start (Linux)
###  Create project directory

```bsh
mkdir -p bc-assessment-service/app/services
cd bc-assessment-service
```

or get from github
```
git clone ......
cd /root/bc-assessment-service


```


### Create virtual environment (`SCRAP`)

```bsh
python3 -m venv SCRAP
source SCRAP/bin/activate
```

Deactivate when done: `deactivate`

### Install dependencies

```bsh
pip install -r requirements.txt
playwright install chromium
```
in linux is better to run 
```bsh
playwright install-deps chromium
```

if fails try :
```bsh
sudo apt install -y libnss3 libatk-bridge2.0-0 libdrm2 libxkbcommon0 libgbm1 libasound2
```


### Environment variables
in `.env.example`:
```bsh
HOST=0.0.0.0
PORT=8011
DATABASE_PATH=data/bc_assessment.db
SCRAPE_CACHE_TTL_SECONDS=0
```
copy and edit:<br>
```bsh
cp .env.example .env
```

### One-time database setup (SQLite cache)

Run once before the first server start:

```bsh
python scripts/setup_db.py
```

This creates `data/bc_assessment.db` with an `assessment_cache` table (`id`, `address`, assessment fields, `created_at`). Every lookup checks this cache first; on a miss it scrapes bcassessment.ca and stores the result. BC Assessment data changes about once per year, so the default TTL is `0` (never expire).

### run the server

Using `.env` for host/port (see `.env.example`):

```bsh
python -m app.main
```

Or with uvicorn directly:

```bsh
uvicorn app.main:app --host 0.0.0.0 --port 8011 --reload
```

Api is accessible by http://localhost:8011/address?address=..

Docds http://localhost:8011/docs


### test 
```bsh
curl -G "http://localhost:8011/address" \
  --data-urlencode "address=7009 TISDALL ST VANCOUVER"
```

The first request after startup may take ~10–20s (browser warmup). Repeat lookups for the same address are instant (SQLite cache). New addresses are typically faster than before (~8–15s) because the scraper reuses one browser tab and skips slow `networkidle` waits.

### Performance tuning (optional `.env`)

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_PATH` | `data/bc_assessment.db` | SQLite cache file path |
| `SCRAPE_CACHE_TTL_SECONDS` | `0` | Cache entry lifetime in seconds (`0` = never expire) |
| `BC_RATE_LIMIT_MAX` | `15` | Short-term max scrapes per window (`0` = disable tier) |
| `BC_RATE_LIMIT_WINDOW_SECONDS` | `60` | Short-term window in seconds |
| `BC_RATE_LIMIT_HOURLY_MAX` | `500` | Hourly max scrapes (`0` = disable tier) |
| `BC_RATE_LIMIT_HOURLY_WINDOW_SECONDS` | `3600` | Hourly window in seconds (1 hour) |
| `BC_RATE_LIMIT_MIN_INTERVAL_SECONDS` | `4` | Minimum seconds between BC requests (`0` = disable) |
| `SCRAPE_WARMUP_ON_START` | `true` | Pre-load bcassessment.ca when the server starts |
| `SCRAPE_AUTOCOMPLETE_TIMEOUT_MS` | `8000` | Max wait for search suggestions |
| `SCRAPE_RESULT_TIMEOUT_MS` | `25000` | Max wait for property page content |
| `TELEGRAM_BOT_TOKEN` | _(empty)_ | Bot token from [@BotFather](https://t.me/BotFather) |
| `TELEGRAM_CHAT_IDS` | _(empty)_ | Comma-separated chat IDs, e.g. `6985166589,109329798` |
| `STATS_MILESTONE_COUNT` | `100` | Reset counters and alert when cache or BC hits reach this |

Copy from `.env.example` into `.env` to override.

### Telegram alerts

Add to `.env` (do **not** commit the real token):

```bash
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_CHAT_IDS=6985166589,109329798
STATS_MILESTONE_COUNT=100
```

In-memory counters track cache hits and BC scrapes. When **either** reaches `STATS_MILESTONE_COUNT`, both counters reset and both chat IDs get a summary message.

Immediate alerts are sent for:

- Empty / whitespace address
- Property not found
- Invalid result (e.g. missing `total_value`)

Check live counts: `GET /stats` → `{"cache_calls": 12, "bc_calls": 3}`





## Production on Linux 
Example unit file /etc/systemd/system/bc-assessment.service:

```bsh
cd /etc/systemd/system/
vim bc-assessment.service
```


```bsh
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

Enable and start 

```bsh
sudo systemctl daemon-reload
sudo systemctl enable bc-assessment
sudo systemctl start bc-assessment
sudo systemctl status bc-assessment

```







----
# Connect BlueNest
In BlueNest backend .env:

```bsh
BC_ASSESSMENT_API_URL=http://YOUR_LINUX_HOST:8011/address
BC_ASSESSMENT_TIMEOUT=30
```

Replace YOUR_LINUX_HOST with server IP or domain. If BlueNest runs in Docker on the same machine, use http://host.docker.internal:8011/address (Mac/Windows) or the Docker network hostname on Linux.




-------
#Probelm : Error

There is one error related to playwrigth that after hours it got stocked and said no proeprty found. so oen of very temporary and easy solution is restart that so I created this


I creatd **Timer unit** at  /etc/systemd/system/bc-assessment-restart.timer

```bsh
[Unit]
Description=Restart BC Assessment API every 2 hours
[Timer]
OnCalendar=*-*-* 00/2:00:00
Persistent=true
[Install]
WantedBy=timers.target
```

And then I created **Servcie Unit** at /etc/systemd/system/bc-assessment-restart.service

```bsh
[Unit]
Description=Restart BC Assessment API (scheduled)
[Service]
Type=oneshot
ExecStart=/usr/bin/systemctl restart bc-assessment.service

```



So for enable and strat
```bsh
sudo systemctl daemon-reload
sudo systemctl enable bc-assessment-restart.timer
sudo systemctl start bc-assessment-restart.timer
```

for checking everytime it is need to see 
```bsh
sudo journalctl -u bc-assessment-restart.service -n 20
sudo systemctl status bc-assessment
```




final test

```bsh
curl -G "http://getplutus.solutions/bc-assessment/address" \
--data-urlencode "address=7009 TISDALL ST VANCOUVER"
```


check timer

```bsh
sudo systemctl status bc-assessment-restart.timer
systemctl list-timers bc-assessment-restart.timer
```


or you can not wait and just easily

```bsh
sudo systemctl start bc-assessment-restart.service
```