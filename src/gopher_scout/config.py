"""Application settings.

Everything is configurable through environment variables prefixed with ``GS_`` (or a
``.env`` file in the project root). Discovery criteria live in ``config/discovery.yaml``
because they are edited more often and read better as YAML.
"""

from __future__ import annotations

import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

EffortLevel = Literal["low", "medium", "high", "xhigh", "max"]


class DiscoveryConfig(BaseModel):
    """Search criteria the Scout agent uses to find candidate repositories."""

    topics: list[str] = Field(default_factory=lambda: ["cli"])
    min_stars: int = 500
    max_stars: int = 20_000
    pushed_within_days: int = 60
    per_topic_limit: int = Field(default=30, ge=1, le=100)
    exclude: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> DiscoveryConfig:
        if not path.exists():
            return cls()
        return cls.model_validate(yaml.safe_load(path.read_text()) or {})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GS_", env_file=PROJECT_ROOT / ".env", extra="ignore"
    )

    data_dir: Path = PROJECT_ROOT / "data"
    workspace_dir: Path = PROJECT_ROOT / "workspace"
    discovery_file: Path = PROJECT_ROOT / "config" / "discovery.yaml"

    github_token: str | None = None

    model: str = "claude-opus-5-5"
    analyst_effort: EffortLevel = "high"
    critic_effort: EffortLevel = "medium"
    implementer_effort: EffortLevel = "high"
    reviewer_effort: EffortLevel = "medium"
    max_budget_usd_per_run: float = 5.0
    max_ideas_per_project: int = 5

    sandbox: Literal["auto", "docker", "local"] = "auto"
    docker_image: str = "golang:1.27"
    command_timeout_s: int = 900

    max_implementation_attempts: int = 3

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.data_dir / 'gopher_scout.db'}"

    @property
    def discovery(self) -> DiscoveryConfig:
        return DiscoveryConfig.load(self.discovery_file)

    def resolve_github_token(self) -> str | None:
        """Prefer an explicit token, otherwise borrow the GitHub CLI's login."""
        if self.github_token:
            return self.github_token
        if shutil.which("gh") is None:
            return None
        result = subprocess.run(
            ["gh", "auth", "token"], capture_output=True, text=True, check=False
        )
        token = result.stdout.strip()
        return token or None

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    return Settings()
