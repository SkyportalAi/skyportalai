"""Skyportal CLI configuration."""

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class PortalConfig(BaseModel):
    """Skyportal application connection settings."""

    base_url: str = Field(
        default="https://app.skyportal.ai",
        description="Skyportal application URL",
    )
    request_timeout: int = Field(
        default=30,
        ge=1,
        description="HTTP request timeout in seconds",
    )


class SkyportalConfig(BaseModel):
    """Top-level CLI configuration."""

    portal: PortalConfig = Field(default_factory=PortalConfig)


class ConfigManager:
    """Load and save Skyportal CLI configuration."""

    DEFAULT_CONFIG_PATH = Path.home() / ".skyportal" / "config.yaml"

    @classmethod
    def get_config_path(cls) -> Path:
        override = os.environ.get("SKYPORTAL_CONFIG_PATH")
        return Path(override).expanduser() if override else cls.DEFAULT_CONFIG_PATH

    @classmethod
    def load_config(cls) -> SkyportalConfig:
        path = cls.get_config_path()
        if not path.exists():
            return SkyportalConfig()
        with path.open() as config_file:
            data = yaml.safe_load(config_file) or {}
        return SkyportalConfig(**data)

    @classmethod
    def save_config(cls, config: SkyportalConfig) -> None:
        path = cls.get_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as config_file:
            yaml.safe_dump(config.model_dump(), config_file, default_flow_style=False)

    @classmethod
    def config_exists(cls) -> bool:
        return cls.get_config_path().exists()
