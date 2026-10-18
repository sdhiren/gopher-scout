"""Logging setup shared by the CLI and the web server."""

from __future__ import annotations

import logging

from rich.logging import RichHandler


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(name)s: %(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
        force=True,
    )
    # Third-party chatter is rarely useful at INFO.
    for noisy in ("httpx", "httpcore", "claude_agent_sdk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
