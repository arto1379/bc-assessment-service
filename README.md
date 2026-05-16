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
├── SCRAP/               # Python virtualenv (local only, do not commit)
├── .env.example
├── .env                 # local only, do not commit
└── app/
    ├── __init__.py
    ├── main.py          # FastAPI routes
    ├── config.py        # host, port
    ├── schemas.py       # response models
    └── services/
        ├── __init__.py
        └── lookup.py    # lookup_by_address() — Main logic (scraping)
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


### Enviuronement variables
in .env.example:
```bsh
HOST=0.0.0.0
PORT=8011
```
copy and edit:<br>
```bsh
cp .env.example .env
```

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

The first request after startup may take ~10–20s (browser warmup). Repeat lookups for the same address are instant (in-memory cache). New addresses are typically faster than before (~8–15s) because the scraper reuses one browser tab and skips slow `networkidle` waits.

### Performance tuning (optional `.env`)

| Variable | Default | Purpose |
|----------|---------|---------|
| `SCRAPE_WARMUP_ON_START` | `true` | Pre-load bcassessment.ca when the server starts |
| `SCRAPE_CACHE_TTL_SECONDS` | `3600` | Cache results per address (set `0` to disable) |
| `SCRAPE_AUTOCOMPLETE_TIMEOUT_MS` | `8000` | Max wait for search suggestions |
| `SCRAPE_RESULT_TIMEOUT_MS` | `25000` | Max wait for property page content |

Copy from `.env.example` into `.env` to override.





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











final test

```bsh
curl -G "http://getplutus.solutions/bc-assessment/address" \
--data-urlencode "address=7009 TISDALL ST VANCOUVER"
```
