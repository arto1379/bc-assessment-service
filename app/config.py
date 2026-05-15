from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    host: str = "0.0.0.0"
    port: int = 8011

    # Scrape performance (see README)
    scrape_warmup_on_start: bool = True
    scrape_cache_ttl_seconds: int = 3600
    scrape_autocomplete_timeout_ms: int = 8_000
    scrape_result_timeout_ms: int = 25_000

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
