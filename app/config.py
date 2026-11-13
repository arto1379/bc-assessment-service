from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8011

    # "live" scrapes bcassessment.ca; "mock" returns fixture data (used by tests)
    lookup_provider: str = "live"

    # SQLite cache (see README — run scripts/setup_db.py once)
    database_path: str = "data/bc_assessment.db"
    # 0 = never expire (BC Assessment updates ~yearly); set e.g. 31536000 for 365 days
    scrape_cache_ttl_seconds: int = 0
    # How long a genuine "not found" is remembered so we stop re-scraping it
    not_found_cache_ttl_seconds: int = 86_400

    # Rate limit for bcassessment.ca (cache hits are not limited)
    bc_rate_limit_max: int = 15
    bc_rate_limit_window_seconds: float = 60.0
    bc_rate_limit_hourly_max: int = 500
    bc_rate_limit_hourly_window_seconds: float = 3600.0
    bc_rate_limit_min_interval_seconds: float = 4.0

    # Scrape performance (see README)
    scrape_warmup_on_start: bool = True
    scrape_autocomplete_timeout_ms: int = 8_000
    scrape_result_timeout_ms: int = 25_000
    # Hard ceiling on one whole lookup so a stuck scrape can never wedge the service
    scrape_total_timeout_ms: int = 60_000
    # Requests allowed to queue for the scrape lock before we fast-fail the caller
    scrape_max_queue_depth: int = 20
    # /address answers `busy` (with Retry-After) instead of waiting longer than
    # this for a rate-limit slot. Bounds the old up-to-an-hour hourly-cap wait.
    scrape_max_projected_wait_seconds: float = 30.0

    # Fraction of BC_RATE_LIMIT_HOURLY_MAX that batch jobs may use (0.7 = 350/500)
    batch_hourly_share: float = 0.7

    # Browser recycling — bounds Chromium memory growth and clears BC's cookies.
    # A fresh BrowserContext is created per lookup regardless of these.
    browser_max_lookups: int = 200
    browser_restart_after_failures: int = 3
    # Consecutive failures before /health reports unhealthy
    unhealthy_after_failures: int = 5

    # Debug snapshots for blocked / unexpected pages
    save_html_snapshots: bool = True
    snapshot_dir: str = "data/snapshots"
    snapshot_keep: int = 50

    # Telegram alerts — set these in .env only. Never commit a real token.
    telegram_bot_token: str = ""
    telegram_chat_ids: str = ""  # comma-separated
    stats_milestone_count: int = 100
    # Minimum seconds between repeat alerts of the same failure type
    alert_min_interval_seconds: float = 300.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def get_telegram_chat_ids(self) -> list[int]:
        if not self.telegram_chat_ids.strip():
            return []
        ids: list[int] = []
        for part in self.telegram_chat_ids.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.append(int(part))
            except ValueError:
                # A malformed chat id must not take the whole service down at import.
                continue
        return ids


settings = Settings()
