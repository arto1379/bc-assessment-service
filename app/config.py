from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8011

    # SQLite cache (see README — run scripts/setup_db.py once)
    database_path: str = "data/bc_assessment.db"
    # 0 = never expire (BC Assessment updates ~yearly); set e.g. 31536000 for 365 days
    scrape_cache_ttl_seconds: int = 0

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

    # Telegram alerts (set in .env — never commit real token)
    telegram_bot_token: str = "8816015658:AAEVkHZWrkl_1qWfvCwOrIJbPMK1QLeQd1I"
    telegram_chat_ids: str = "6985166589,109329798"  # comma-separated
    stats_milestone_count: int = 100

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def get_telegram_chat_ids(self) -> list[int]:
        if not self.telegram_chat_ids.strip():
            return []
        return [
            int(part.strip())
            for part in self.telegram_chat_ids.split(",")
            if part.strip()
        ]


settings = Settings()
