"""Local git operations.

Two guarantees are enforced here rather than trusted to an LLM:

* **Nothing is ever pushed.** Every clone gets its push URL replaced with an invalid
  value, so even an accidental ``git push`` fails. You push manually when ready.
* **Commits carry only your identity.** Commits are made by this code (not the agent),
  using your git config, and any ``Co-Authored-By`` / "Generated with" trailers are
  stripped from the message.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

from gopher_scout.services.process import CommandResult, run

PUSH_DISABLED_URL = "PUSH_DISABLED_BY_GOPHER_SCOUT"
LOCAL_EXCLUDES = (".gs-cache/",)

_TRAILER_RE = re.compile(
    r"^\s*(co-authored-by:.*|generated[- ]with.*|🤖.*|signed-off-by:\s*claude.*)$",
    re.IGNORECASE,
)


def sanitize_commit_message(message: str) -> str:
    """Drop AI attribution trailers and normalise whitespace."""
    kept = [line.rstrip() for line in message.strip().splitlines() if not _TRAILER_RE.match(line)]
    text = "\n".join(kept).strip()
    return re.sub(r"\n{3,}", "\n\n", text) + "\n"


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "change"


class GitRepo:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def _git(self, *args: str, check: bool = True, timeout: float = 600) -> CommandResult:
        return await run(["git", *args], cwd=self.path, timeout=timeout, check=check)

    # ------------------------------------------------------------------ setup

    @classmethod
    async def clone_or_update(cls, clone_url: str, dest: Path, default_branch: str) -> GitRepo:
        if (dest / ".git").exists():
            repo = cls(dest)
            await repo._git("fetch", "--prune", "origin", default_branch)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Blobless clone: full history for branching/blame, file contents fetched lazily.
            await run(
                ["git", "clone", "--filter=blob:none", "--no-tags", clone_url, str(dest)],
                timeout=900,
                check=True,
            )
            repo = cls(dest)
        await repo.harden()
        return repo

    async def harden(self) -> None:
        await self._git("remote", "set-url", "--push", "origin", PUSH_DISABLED_URL)
        exclude = self.path / ".git" / "info" / "exclude"
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text() if exclude.exists() else ""
        missing = [p for p in LOCAL_EXCLUDES if p not in existing]
        if missing:
            exclude.write_text(existing.rstrip("\n") + "\n" + "\n".join(missing) + "\n")

    # ------------------------------------------------------------------ queries

    async def head_sha(self, ref: str = "HEAD") -> str:
        return (await self._git("rev-parse", ref)).stdout.strip()

    async def current_branch(self) -> str:
        return (await self._git("rev-parse", "--abbrev-ref", "HEAD")).stdout.strip()

    async def is_dirty(self) -> bool:
        return bool((await self._git("status", "--porcelain")).stdout.strip())

    async def diff(self, base: str) -> str:
        # Include untracked files by staging intent-to-add first.
        await self._git("add", "--intent-to-add", "--all")
        return (await self._git("diff", base)).stdout

    async def diff_stat(self, base: str) -> str:
        await self._git("add", "--intent-to-add", "--all")
        return (await self._git("diff", "--stat", base)).stdout.strip()

    async def changed_files(self, base: str) -> list[str]:
        await self._git("add", "--intent-to-add", "--all")
        out = (await self._git("diff", "--name-only", "--diff-filter=d", base)).stdout
        return [line for line in out.splitlines() if line]

    async def add_worktree(self, path: Path, ref: str) -> None:
        await self._git("worktree", "add", "--force", "--detach", str(path), ref)

    async def remove_worktree(self, path: Path) -> None:
        await self._git("worktree", "remove", "--force", str(path), check=False)

    async def push_disabled(self) -> bool:
        res = await self._git("remote", "get-url", "--push", "origin", check=False)
        return res.stdout.strip() == PUSH_DISABLED_URL

    # ------------------------------------------------------------------ mutations

    async def checkout_default(self, default_branch: str) -> None:
        await self._git("checkout", "--force", default_branch)
        await self._git("reset", "--hard", f"origin/{default_branch}")
        await self._git("clean", "-fdx", "-e", ".gs-cache")

    async def create_branch(self, branch: str, base: str) -> None:
        await self._git("checkout", "-B", branch, base)

    async def switch(self, branch: str) -> None:
        await self._git("checkout", branch)

    async def commit_all(self, message: str) -> str:
        await self._git("add", "--all")
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write(sanitize_commit_message(message))
            msg_path = fh.name
        try:
            await self._git("commit", "--file", msg_path)
        finally:
            Path(msg_path).unlink(missing_ok=True)
        return await self.head_sha()
