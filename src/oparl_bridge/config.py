from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPARL_", env_file=".env", extra="ignore")

    allris_base_url: str = "https://www.neu-wulmstorf.de/allris"
    database_url: str = "sqlite:///./oparl_bridge.db"
    api_base_url: str = "http://localhost:8000"
    body_name: str = "Gemeinde Neu Wulmstorf"
    body_website: str = "https://www.neu-wulmstorf.de"
    scraper_timeout_ms: int = 30000
    scraper_headless: bool = True


settings = Settings()
