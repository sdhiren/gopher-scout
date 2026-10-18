"""Runs build/test commands for untrusted repositories.

``go test`` executes code written by strangers, so by default it runs inside a
throwaway Docker container with only the repo mounted. ``local`` mode exists for
speed when you trust the project.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from gopher_scout.services.process import CommandResult, run

log = logging.getLogger(__name__)

SandboxMode = Literal["docker", "local"]


def go_cache_env(repo: Path) -> dict[str, str]:
    """Keep Go caches inside the (git-excluded) repo so sandboxed processes can write them."""
    cache = repo / ".gs-cache"
    return {
        "GOCACHE": str(cache / "build"),
        "GOMODCACHE": str(cache / "mod"),
        "GOPATH": str(cache / "gopath"),
        "GOFLAGS": "-buildvcs=false -modcacherw",  # .git may be unreadable in the container
        "GOTOOLCHAIN": "local",
    }


async def resolve_mode(preference: Literal["auto", "docker", "local"]) -> SandboxMode:
    if preference != "auto":
        return preference
    if shutil.which("docker") and (await run(["docker", "info"], timeout=20)).ok:
        return "docker"
    log.warning("Docker unavailable; build/test will run directly on this machine")
    return "local"


class Sandbox:
    def __init__(self, mode: SandboxMode, image: str, timeout: float) -> None:
        self.mode = mode
        self.image = image
        self.timeout = timeout

    async def run(self, repo: Path, command: Sequence[str]) -> CommandResult:
        if self.mode == "local":
            return await run(command, cwd=repo, env=go_cache_env(repo), timeout=self.timeout)
        env = go_cache_env(Path("/src"))
        docker = [
            "docker", "run", "--rm",
            "--cpus", "4", "--memory", "4g", "--pids-limit", "1024",
            "--security-opt", "no-new-privileges",
            "-v", f"{repo}:/src", "-w", "/src",
            *[arg for k, v in env.items() for arg in ("-e", f"{k}={v}")],
            self.image, *command,
        ]  # fmt: skip
        return await run(docker, timeout=self.timeout)
