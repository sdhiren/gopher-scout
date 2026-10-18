from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from gopher_scout.config import Settings, get_settings
from gopher_scout.db import repository as repo
from gopher_scout.db import session as db_session
from gopher_scout.domain.enums import ContributionStrategy, Effort, IdeaCategory, IdeaStatus
from gopher_scout.domain.schemas import Evidence, IdeaProposal


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Settings]:
    """Isolated settings + a fresh SQLite database per test."""
    monkeypatch.setenv("GS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GS_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("GS_GITHUB_TOKEN", "test-token")
    monkeypatch.setenv("GS_SANDBOX", "local")
    get_settings.cache_clear()
    db_session.get_engine.cache_clear()
    db_session._factory.cache_clear()
    s = get_settings()
    s.ensure_dirs()
    db_session.init_db()
    yield s
    db_session.get_engine().dispose()
    get_settings.cache_clear()
    db_session.get_engine.cache_clear()
    db_session._factory.cache_clear()


def make_proposal(**overrides: object) -> IdeaProposal:
    data: dict[str, object] = {
        "title": "Return non-zero exit code when a subcommand fails",
        "category": IdeaCategory.UX,
        "summary": "Scripts cannot detect failures because the tool always exits 0.",
        "problem_statement": "When `tool sync` fails it prints an error but exits 0.",
        "evidence": [
            Evidence(
                file="cmd/root.go",
                line_start=10,
                line_end=12,
                snippet="os.Exit(0)",
                explanation="Exit code is hard-coded.",
            )
        ],
        "root_cause": "Execute() errors are logged and swallowed.",
        "proposed_solution": "Propagate the error from Execute() and exit 1.",
        "benefits": ["CI pipelines can detect failures"],
        "risks": "Scripts that relied on exit 0 will now see failures.",
        "backward_compatible": False,
        "effort": Effort.S,
        "acceptance_criteria": ["`tool bad-cmd; echo $?` prints 1"],
        "contribution_strategy": ContributionStrategy.DIRECT_PR,
    }
    data.update(overrides)
    return IdeaProposal.model_validate(data)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def origin_repo(tmp_path: Path) -> Path:
    """A local 'upstream' repository with one commit on main."""
    src = tmp_path / "origin"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    git(src, "config", "user.name", "Test User")
    git(src, "config", "user.email", "test@example.com")
    (src / "go.mod").write_text("module example.com/tool\n\ngo 1.22\n")
    (src / "main.go").write_text('package main\n\nfunc main() { println("hi") }\n')
    git(src, "add", ".")
    git(src, "commit", "-q", "-m", "initial")
    return src


@pytest.fixture
def seeded(settings: Settings) -> tuple[int, int]:
    """One project with one idea awaiting review. Returns (project_id, idea_id)."""
    with db_session.session_scope() as s:
        project, _ = repo.upsert_project(
            s, "acme/tool", html_url="https://github.com/acme/tool", stars=1200
        )
        idea = repo.add_idea(s, project, None, make_proposal())
        idea.status = IdeaStatus.AWAITING_REVIEW
        return project.id, idea.id
