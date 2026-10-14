from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)
    database_url: SecretStr = SecretStr("sqlite:///./channel-radar.db")
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = Field("gemini-3.1-flash-lite", pattern=r"^[a-zA-Z0-9._-]+$")
    scheduler_token: SecretStr = SecretStr("")
    public_url: str = "http://127.0.0.1:8000"
    collection_interval_seconds: int = Field(3600, ge=300)
    worker_enabled: bool = True
    max_channels: int = Field(10, ge=1, le=30)
    ai_daily_limit: int = Field(20, ge=1, le=100)
    environment: str = "development"

    @model_validator(mode="after")
    def production_requirements(self):
        if self.environment == "production":
            if not self.database_url.get_secret_value().startswith(("postgres:", "postgresql")):
                raise ValueError("Production requires PostgreSQL")
            if len(self.scheduler_token.get_secret_value()) < 32:
                raise ValueError("Production requires a scheduler token of at least 32 characters")
            if not self.public_url.startswith("https://"):
                raise ValueError("Production PUBLIC_URL must use HTTPS")
        return self
