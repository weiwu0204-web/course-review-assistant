"""Settings used only by the service entrypoints."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.config import Settings


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PPT_SERVICE_", env_file=".service.env",
                                      env_ignore_empty=True, extra="ignore")

    database_url: SecretStr
    object_backend: Literal["s3", "local"] = "s3"
    local_objects: Path = Path(".service-data/objects")
    s3_bucket: str = "ppt-reviews"
    s3_endpoint: str | None = None
    s3_region: str = "us-east-1"
    s3_access_key: SecretStr | None = None
    s3_secret_key: SecretStr | None = None
    upload_limit: int = Field(default=50 * 1024**2, gt=0, le=100 * 1024**2)
    session_ttl: int = Field(default=8 * 3600, gt=0)
    lease_seconds: int = Field(default=60, ge=3)
    generator: Literal["demo", "model"] = "demo"
    model_api_key: SecretStr | None = None
    model_base_url: str = "https://api.deepseek.com"
    model_name: str = ""
    model_vision_name: str = ""
    model_max_tokens: int = Field(default=16384, gt=0)
    model_timeout: int = Field(default=300, gt=0, le=600)
    model_price_input: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    model_price_output: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    def repair_pricing(self) -> dict[str, float] | None:
        if self.generator != "model" or self.model_price_input is None or self.model_price_output is None:
            return None
        return {"price_in": self.model_price_input, "price_out": self.model_price_output}

    def model_settings(self) -> Settings | None:
        if self.generator == "demo" or not self.model_api_key or not self.model_name:
            return None
        return Settings(
            _env_file=None, ai_api_key=self.model_api_key.get_secret_value(), ai_base_url=self.model_base_url,
            ai_model=self.model_name, ai_vision_model=self.model_vision_name, ai_thinking_off={},
            ai_max_tokens=self.model_max_tokens, ai_timeout_seconds=self.model_timeout, ai_max_retries=1,
        )
