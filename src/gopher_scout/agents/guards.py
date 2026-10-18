"""PreToolUse guard that keeps LLM agents inside an explicit Bash allowlist.

The Implementer needs to run ``go build`` / ``go test``; it must never push, commit,
rewrite remotes, or reach for the network. Commits are done by our own code (see
``services.git``) so that no AI co-author trailer is ever added.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable
from typing import Any

from claude_agent_sdk import HookContext, HookMatcher

_SEGMENT_SPLIT = re.compile(r"\|\||&&|;|\||\n")
_FORBIDDEN_SYNTAX = ("$(", "`", ">", "<(")

# (command, allowed first sub-arguments or None for any)
DEFAULT_ALLOWED: dict[str, frozenset[str] | None] = {
    "go": frozenset({"build", "test", "vet", "fmt", "list", "doc", "env", "version", "mod"}),
    "gofmt": None,
    "goimports": None,
    "staticcheck": None,
    "git": frozenset({"status", "diff", "log", "show", "blame", "grep", "ls-files"}),
    "ls": None,
    "wc": None,
    "head": None,
    "tail": None,
    "sort": None,
    "uniq": None,
    "grep": None,
    "rg": None,
    "find": None,
    "cd": None,
}


def check_command(command: str, allowed: dict[str, frozenset[str] | None]) -> str | None:
    """Return a denial reason, or ``None`` if every segment is allowed."""
    command = command.replace("2>&1", "")  # merging stderr is harmless and very common
    if any(tok in command for tok in _FORBIDDEN_SYNTAX):
        return "command substitution and redirection are not allowed"
    for segment in _SEGMENT_SPLIT.split(command):
        segment = segment.strip()
        if not segment:
            continue
        try:
            words = shlex.split(segment)
        except ValueError:
            return f"could not parse `{segment}`"
        # Allow leading env assignments like GOFLAGS=-count=1 go test
        while words and re.fullmatch(r"[A-Z_][A-Z0-9_]*=\S*", words[0]):
            words.pop(0)
        if not words:
            continue
        cmd, rest = words[0], words[1:]
        if cmd not in allowed:
            return f"`{cmd}` is not on the allowlist"
        subs = allowed[cmd]
        if subs is not None and (not rest or rest[0] not in subs):
            return f"`{cmd} {rest[0] if rest else ''}` is not allowed"
        if cmd == "find" and any(a in {"-exec", "-execdir", "-delete", "-ok"} for a in rest):
            return "`find` with -exec/-delete is not allowed"
        if cmd == "go" and rest and rest[0] == "mod" and (len(rest) < 2 or rest[1] != "tidy"):
            return "only `go mod tidy` is allowed"
    return None


def bash_allowlist_hook(
    allowed: dict[str, frozenset[str] | None] | None = None,
) -> dict[Any, list[HookMatcher]]:
    rules = allowed or DEFAULT_ALLOWED

    async def guard(input_data: Any, _tool_use_id: str | None, _ctx: HookContext) -> Any:
        command = str(input_data.get("tool_input", {}).get("command", ""))
        reason = check_command(command, rules)
        if reason is None:
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"Blocked by gopher-scout: {reason}.",
            }
        }

    return {"PreToolUse": [HookMatcher(matcher="Bash", hooks=[guard])]}


def deny_patterns(extra: Iterable[str] = ()) -> list[str]:
    """Permission-rule denials applied on top of the hook (defence in depth)."""
    return [
        "Bash(git push:*)",
        "Bash(git commit:*)",
        "Bash(git remote:*)",
        "Bash(git config:*)",
        "Bash(gh:*)",
        "Bash(curl:*)",
        "Bash(wget:*)",
        "WebFetch",
        "WebSearch",
        *extra,
    ]
