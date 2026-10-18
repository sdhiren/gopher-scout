from __future__ import annotations

import subprocess
from pathlib import Path

from gopher_scout.services.git import GitRepo, sanitize_commit_message, slugify
from tests.conftest import git


def test_sanitize_strips_ai_trailers() -> None:
    msg = (
        "fix(cli): return exit code 1 on failure\n\n"
        "Body line.\n\n"
        "Co-Authored-By: Claude <noreply@anthropic.com>\n"
        "🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"
    )
    clean = sanitize_commit_message(msg)
    assert "Co-Authored-By" not in clean
    assert "Generated with" not in clean
    assert clean == "fix(cli): return exit code 1 on failure\n\nBody line.\n"


def test_slugify() -> None:
    assert (
        slugify("Return non-zero exit code (when sync fails)!", max_len=60)
        == "return-non-zero-exit-code-when-sync-fails"
    )


async def test_clone_disables_push_and_commits_as_user(origin_repo: Path, tmp_path: Path) -> None:
    dest = tmp_path / "ws" / "acme" / "tool"
    repo = await GitRepo.clone_or_update(origin_repo.as_uri(), dest, "main")
    assert await repo.push_disabled()
    git(dest, "config", "user.name", "Dhiren Test")
    git(dest, "config", "user.email", "dhiren@example.com")

    base = await repo.head_sha()
    await repo.create_branch("gs/1-exit-code", base)
    (dest / "main.go").write_text('package main\n\nfunc main() { println("bye") }\n')
    (dest / "new_test.go").write_text("package main\n")
    (dest / ".gs-cache").mkdir()
    (dest / ".gs-cache" / "junk").write_text("cache")

    assert await repo.changed_files(base) == ["main.go", "new_test.go"]
    sha = await repo.commit_all("feat: bye\n\nCo-Authored-By: Claude <noreply@anthropic.com>")

    message = git(dest, "log", "-1", "--format=%B")
    assert "Co-Authored-By" not in message
    assert git(dest, "log", "-1", "--format=%an <%ae>") == "Dhiren Test <dhiren@example.com>"
    assert ".gs-cache/junk" not in git(dest, "show", "--name-only", "--format=", sha)
    assert await repo.current_branch() == "gs/1-exit-code"

    push = subprocess.run(  # noqa: ASYNC221 - a plain blocking call is fine in a test
        ["git", "push", "origin", "HEAD"], cwd=dest, capture_output=True
    )
    assert push.returncode != 0, "pushing must be impossible"
