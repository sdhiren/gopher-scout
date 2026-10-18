"""Async subprocess helper used by git, Go tooling and the sandbox."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        return (self.stdout + ("\n" if self.stdout and self.stderr else "") + self.stderr).strip()


class CommandError(RuntimeError):
    def __init__(self, result: CommandResult) -> None:
        super().__init__(
            f"`{' '.join(result.args)}` failed ({result.returncode}): {result.output[-800:]}"
        )
        self.result = result


async def run(
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float = 600,
    check: bool = False,
) -> CommandResult:
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env={**os.environ, **(env or {})},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    timed_out = False
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        proc.kill()
        out, err = await proc.communicate()
    result = CommandResult(
        args=tuple(args),
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=out.decode("utf-8", errors="replace"),
        stderr=err.decode("utf-8", errors="replace"),
        timed_out=timed_out,
    )
    if check and not result.ok:
        raise CommandError(result)
    return result
