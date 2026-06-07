from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPARL_", env_file=".env", extra="ignore")

    allris_base_url: str = "https://your-municipality.invalid/allris"
    database_url: str = "sqlite:///./oparl_bridge.db"
    api_base_url: str = "http://localhost:8000"
    body_name: str = "Gemeinde Musterstadt"
    body_website: str = "https://your-municipality.invalid"
    system_name: str = "Bürgerinformationssystem"
    wikidata_id: str | None = None
    favicon_b64: str | None = None
    scraper_timeout_ms: int = 30000
    scraper_headless: bool = True
    scraper_delay_ms: int = 1500


settings = Settings()
