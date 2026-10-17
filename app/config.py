from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    processing_delay_seconds: float = 30.0
    worker_concurrency: int = 1000
    worker_idle_poll_seconds: float = 600.0
    max_attempts: int = 5
    rate_limit_enabled: bool = True
    rate_limit_per_second: float = 200.0
    rate_limit_burst: int = 400


settings = Settings()
