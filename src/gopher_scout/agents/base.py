"""Shared machinery for all agents.

* :class:`Agent` — common identity + logger.
* :class:`LLMAgent` — runs a Claude Agent SDK session with a locked-down tool set and
  returns a validated Pydantic model via structured output. Cost and outcome of every
  call are recorded in the database.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, query
from pydantic import BaseModel, ValidationError

from gopher_scout.config import EffortLevel, Settings
from gopher_scout.db import repository as repo
from gopher_scout.db.session import session_scope

# Claude Code settings applied to every agent session. Attribution is disabled so
# nothing the agent writes carries a Claude co-author trailer.
_SESSION_SETTINGS = json.dumps(
    {"includeCoAuthoredBy": False, "attribution": {"commit": "", "pr": ""}}
)


def load_prompt(name: str) -> str:
    return resources.files("gopher_scout.agents.prompts").joinpath(f"{name}.md").read_text()


class AgentOutputError(RuntimeError):
    """The agent finished without producing valid structured output."""


@dataclass(frozen=True)
class RunStats:
    cost_usd: float
    num_turns: int
    duration_ms: int


class Agent:
    name: str = "agent"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.log = logging.getLogger(f"gopher_scout.{self.name}")


class LLMAgent(Agent):
    max_turns: int = 60

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        # Each agent's effort is configurable as GS_<NAME>_EFFORT.
        self.effort: EffortLevel = getattr(settings, f"{self.name}_effort", "high")

    async def run_structured[T: BaseModel](
        self,
        *,
        prompt: str,
        output: type[T],
        cwd: Path,
        target: str,
        tools: list[str],
        system_prompt: str,
        hooks: dict[Any, list[HookMatcher]] | None = None,
        disallowed_tools: list[str] | None = None,
        sandbox: dict[str, Any] | None = None,
        env: dict[str, str] | None = None,
        extra_dirs: list[Path] | None = None,
    ) -> T:
        options = ClaudeAgentOptions(
            model=self.settings.model,
            effort=self.effort,
            system_prompt={"type": "preset", "preset": "claude_code", "append": system_prompt},
            cwd=str(cwd),
            tools=tools,
            allowed_tools=tools,
            disallowed_tools=disallowed_tools or [],
            # Anything not explicitly allowed is denied instead of prompting.
            permission_mode="dontAsk",
            max_turns=self.max_turns,
            max_budget_usd=self.settings.max_budget_usd_per_run,
            output_format={"type": "json_schema", "schema": output.model_json_schema()},
            # Isolate from your personal ~/.claude config and the target repo's .claude dir.
            setting_sources=[],
            settings=_SESSION_SETTINGS,
            hooks=hooks,
            sandbox=sandbox,  # type: ignore[arg-type]
            env=env or {},
            add_dirs=[str(d) for d in extra_dirs or []],
        )

        self.log.info("starting session for %s (effort=%s)", target, self.effort)
        result: ResultMessage | None = None
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, ResultMessage):
                    result = message
            parsed = self._parse(result, output)
        except Exception as exc:
            self._record(target, result, ok=False, error=str(exc)[:2000])
            raise
        self._record(target, result, ok=True)
        return parsed

    def _parse[T: BaseModel](self, result: ResultMessage | None, output: type[T]) -> T:
        if result is None:
            raise AgentOutputError("session ended without a result message")
        if result.is_error:
            detail = "; ".join(result.errors or []) or result.subtype
            raise AgentOutputError(f"agent session failed: {detail}")
        payload = result.structured_output
        if payload is None and result.result:
            # Fall back to a JSON final message if structured output was not populated.
            try:
                payload = json.loads(result.result)
            except json.JSONDecodeError as exc:
                raise AgentOutputError("agent returned no structured output") from exc
        try:
            return output.model_validate(payload)
        except ValidationError as exc:
            raise AgentOutputError(f"agent output failed validation: {exc}") from exc

    def _record(
        self, target: str, result: ResultMessage | None, *, ok: bool, error: str | None = None
    ) -> RunStats:
        stats = RunStats(
            cost_usd=(result.total_cost_usd or 0.0) if result else 0.0,
            num_turns=result.num_turns if result else 0,
            duration_ms=result.duration_ms if result else 0,
        )
        with session_scope() as s:
            repo.record_agent_run(
                s,
                self.name,
                target,
                ok=ok,
                cost_usd=stats.cost_usd,
                num_turns=stats.num_turns,
                duration_ms=stats.duration_ms,
                error=error,
            )
        self.log.info(
            "%s %s: $%.3f, %d turns, %.0fs",
            target,
            "ok" if ok else "FAILED",
            stats.cost_usd,
            stats.num_turns,
            stats.duration_ms / 1000,
        )
        return stats
