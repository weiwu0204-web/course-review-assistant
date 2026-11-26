"""Settings used only by the service entrypoints."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServiceSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PPT_SERVICE_", env_file=".service.env", extra="ignore")

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
