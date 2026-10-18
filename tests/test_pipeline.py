"""End-to-end orchestration tests with the LLM agents replaced by fakes."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from gopher_scout.agents.implementer import ImplementerAgent
from gopher_scout.agents.reviewer import ReviewerAgent
from gopher_scout.config import Settings
from gopher_scout.db import repository as repo
from gopher_scout.db.session import session_scope
from gopher_scout.domain.enums import CriticVerdict, IdeaStatus, ImplementationStatus
from gopher_scout.domain.schemas import (
    CheckResult,
    CritiqueResult,
    ImplementationReport,
    ReviewReport,
)
from gopher_scout.orchestrator.pipeline import Pipeline, WorkspaceDirtyError
from gopher_scout.services.git import GitRepo
from tests.conftest import git, make_proposal

REPORT = ImplementationReport(
    summary="Exit with status 1 when a command fails.",
    commit_subject="fix(cli): exit non-zero on command failure",
    commit_body=(
        "Scripts could not detect failures.\n\nCo-Authored-By: Claude <noreply@anthropic.com>"
    ),
    verification="go test ./... ok",
)


async def _prepare(settings: Settings, seeded: tuple[int, int], origin_repo: Path) -> Path:
    project_id, idea_id = seeded
    dest = settings.workspace_dir / "acme" / "tool"
    await GitRepo.clone_or_update(origin_repo.as_uri(), dest, "main")
    git(dest, "config", "user.name", "Dhiren Test")
    git(dest, "config", "user.email", "dhiren@example.com")
    with session_scope() as s:
        project = repo.get_project(s, project_id)
        assert project is not None
        project.local_path = str(dest)
    Pipeline(settings).decide(idea_id, IdeaStatus.APPROVED, "keep flags unchanged")
    return dest


async def test_implement_commits_locally_without_coauthor(
    settings: Settings, seeded: tuple[int, int], origin_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, idea_id = seeded
    dest = await _prepare(settings, seeded, origin_repo)
    prompts: list[str | None] = []

    async def fake_implement(self, idea, repo_path, feedback=None):  # type: ignore[no-untyped-def]
        prompts.append(feedback)
        (repo_path / "main.go").write_text("package main\n\nfunc main() { panic(1) }\n")
        assert idea.user_notes == "keep flags unchanged"
        return REPORT

    reviews = iter(
        [
            ReviewReport(
                checks=[CheckResult(name="test", command="go test", ok=False, output="FAIL x")]
            ),
            ReviewReport(checks=[CheckResult(name="test", command="go test", ok=True)]),
        ]
    )

    async def fake_review(self, idea, git_repo, base_sha):  # type: ignore[no-untyped-def]
        return next(reviews)

    monkeypatch.setattr(ImplementerAgent, "run", fake_implement)
    monkeypatch.setattr(ReviewerAgent, "run", fake_review)

    impl_id = await Pipeline(settings).implement(idea_id)

    # The second attempt received the first review's failure as feedback.
    assert prompts[0] is None and "FAIL x" in (prompts[1] or "")
    with session_scope() as s:
        impl = repo.get_implementation(s, impl_id)
        assert impl is not None
        assert impl.status == ImplementationStatus.PASSED
        assert impl.attempts == 2
        assert impl.idea.status == IdeaStatus.READY_FOR_PR
        branch, sha = impl.branch, impl.commit_sha

    assert branch.startswith(f"gs/{idea_id}-return-non-zero-exit-code")
    assert git(dest, "rev-parse", "--abbrev-ref", "HEAD") == branch
    assert git(dest, "rev-parse", "HEAD") == sha
    message = git(dest, "log", "-1", "--format=%B")
    assert message.startswith("fix(cli): exit non-zero on command failure")
    assert "Co-Authored-By" not in message
    assert git(dest, "log", "-1", "--format=%an") == "Dhiren Test"
    assert git(dest, "remote", "get-url", "--push", "origin").startswith("PUSH_DISABLED")
    # Upstream is untouched.
    assert git(origin_repo, "branch", "--list", branch) == ""


async def test_failed_review_marks_needs_attention(
    settings: Settings, seeded: tuple[int, int], origin_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, idea_id = seeded
    await _prepare(settings, seeded, origin_repo)

    async def fake_implement(self, idea, repo_path, feedback=None):  # type: ignore[no-untyped-def]
        (repo_path / "main.go").write_text("package main\n// broken\n")
        return REPORT

    async def fake_review(self, idea, git_repo, base_sha):  # type: ignore[no-untyped-def]
        return ReviewReport(checks=[CheckResult(name="build", command="go build", ok=False)])

    monkeypatch.setattr(ImplementerAgent, "run", fake_implement)
    monkeypatch.setattr(ReviewerAgent, "run", fake_review)

    impl_id = await Pipeline(settings).implement(idea_id)
    with session_scope() as s:
        impl = repo.get_implementation(s, impl_id)
        assert impl is not None
        assert impl.status == ImplementationStatus.FAILED
        assert impl.attempts == settings.max_implementation_attempts
        assert impl.idea.status == IdeaStatus.NEEDS_ATTENTION
        assert impl.commit_sha  # work is preserved on the branch for you to inspect


async def test_implement_refuses_unapproved_and_dirty(
    settings: Settings, seeded: tuple[int, int], origin_repo: Path
) -> None:
    _, idea_id = seeded
    with pytest.raises(ValueError, match="approve it first"):
        await Pipeline(settings).implement(idea_id)

    dest = await _prepare(settings, seeded, origin_repo)
    (dest / "main.go").write_text("// my local edit\n")
    with pytest.raises(WorkspaceDirtyError):
        await Pipeline(settings).implement(idea_id)
    assert (dest / "main.go").read_text() == "// my local edit\n"  # never clobbered


def critique(impact: int, confidence: int, merge: int) -> CritiqueResult:
    return CritiqueResult(
        verdict=CriticVerdict.ACCEPT,
        impact_score=impact,
        confidence_score=confidence,
        merge_likelihood=merge,
        reasoning="checked",
    )


def test_critic_threshold_routes_ideas(settings: Settings, seeded: tuple[int, int]) -> None:
    project_id, _ = seeded
    with session_scope() as s:
        project = repo.get_project(s, project_id)
        assert project is not None
        weak = repo.add_idea(s, project, None, make_proposal(title="Weak idea"))
        strong = repo.add_idea(s, project, None, make_proposal(title="Strong idea"))
        repo.apply_critique(s, weak, critique(3, 4, 3), accept_threshold=5.0)
        repo.apply_critique(s, strong, critique(8, 9, 8), accept_threshold=5.0)
        assert weak.status == IdeaStatus.CRITIC_REJECTED
        assert strong.status == IdeaStatus.AWAITING_REVIEW


async def test_scout_stores_projects(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/search/repositories"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "full_name": "acme/tool",
                        "html_url": "https://github.com/acme/tool",
                        "stargazers_count": 2500,
                        "pushed_at": "2026-10-10T00:00:00Z",
                        "open_issues_count": 40,
                        "default_branch": "main",
                        "license": {"spdx_id": "MIT"},
                        "topics": ["cli"],
                    },
                    {"full_name": "cli/cli", "html_url": "x", "stargazers_count": 10},
                ]
            },
        )

    from gopher_scout.agents.scout import ScoutAgent
    from gopher_scout.services.github import GitHubClient

    monkeypatch.setattr(settings, "discovery_file", Path("/nonexistent"))  # default config
    async with GitHubClient("t", transport=httpx.MockTransport(handler)) as gh:
        first = await ScoutAgent(settings).run(gh)
        again = await ScoutAgent(settings).run(gh)
    assert (first.new, again.new) == (2, 0)
    with session_scope() as s:
        project = repo.get_project_by_name(s, "acme/tool")
        assert project is not None and project.license == "MIT" and project.selection_score > 0
