from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from gopher_scout.config import Settings
from gopher_scout.db import repository as repo
from gopher_scout.db.session import session_scope
from gopher_scout.domain.enums import IdeaStatus
from gopher_scout.orchestrator.pipeline import Pipeline
from gopher_scout.web.app import create_app


def test_pages_render_and_approval_flow(settings: Settings, seeded: tuple[int, int]) -> None:
    project_id, idea_id = seeded
    client = TestClient(create_app())

    for url in (
        "/",
        "/ideas",
        f"/ideas?project={project_id}",
        "/projects",
        f"/projects/{project_id}",
        "/jobs",
    ):
        resp = client.get(url)
        assert resp.status_code == 200, url

    project_page = client.get(f"/projects/{project_id}").text
    assert "View on GitHub" in project_page
    assert 'href="https://github.com/acme/tool"' in project_page
    # Profile and Analyse open the styled confirmation dialog instead of running directly.
    assert "data-confirm=" in project_page and 'id="gs-confirm"' in project_page
    assert "Profile this project to see the opening of its README" in project_page

    page = client.get(f"/ideas/{idea_id}").text
    assert "Return non-zero exit code" in page
    assert "Scripts cannot detect failures" in page
    assert "https://github.com/acme/tool/blob/main/cmd/root.go#L10-L12" in page
    assert 'value="approved"' in page

    resp = client.post(
        f"/ideas/{idea_id}/decision",
        data={"decision": "approved", "notes": "Add a test for the help command too"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with session_scope() as s:
        idea = repo.get_idea(s, idea_id)
        assert idea is not None
        assert idea.status == IdeaStatus.APPROVED
        assert idea.user_notes == "Add a test for the help command too"

    assert "Start implementation" in client.get(f"/ideas/{idea_id}").text


def test_invalid_decision_is_rejected(settings: Settings, seeded: tuple[int, int]) -> None:
    _, idea_id = seeded
    client = TestClient(create_app())
    resp = client.post(f"/ideas/{idea_id}/decision", data={"decision": "merged"})
    assert resp.status_code == 400
    assert client.get("/ideas/999").status_code == 404


def test_stage_buttons_run_jobs_and_ignore_double_clicks(
    settings: Settings, seeded: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    async def fake_discover(self: Pipeline) -> str:
        calls.append("discover")
        await asyncio.sleep(0.3)
        return "10 matched"

    monkeypatch.setattr(Pipeline, "discover", fake_discover)
    with TestClient(create_app()) as client:
        for _ in range(2):  # second click lands while the first job is still running
            resp = client.post("/actions/discover", follow_redirects=False)
            assert resp.status_code == 303
            assert resp.headers["location"] == "/jobs"
        assert "Discover…" in client.get("/").text  # button shows it is running
        deadline = time.monotonic() + 5
        while "badge done" not in client.get("/jobs").text:
            assert time.monotonic() < deadline, "job never finished"
            time.sleep(0.05)
    assert calls == ["discover"]
    assert "10 matched" in client.get("/jobs").text


def test_new_ideas_show_in_needs_critique_and_can_be_critiqued(
    settings: Settings, seeded: tuple[int, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.conftest import make_proposal

    project_id, _ = seeded
    with session_scope() as s:
        project = repo.get_project(s, project_id)
        assert project is not None
        new_ids = [
            repo.add_idea(s, project, None, make_proposal(title=f"New idea {n}")).id for n in (1, 2)
        ]

    critiqued: list[list[int] | None] = []

    async def fake_critique(self: Pipeline, idea_ids: list[int] | None = None) -> str:
        critiqued.append(list(idea_ids) if idea_ids else None)
        return "ok"

    monkeypatch.setattr(Pipeline, "critique", fake_critique)
    with TestClient(create_app()) as client:
        board = client.get("/ideas").text
        needs = board.split('aria-label="Needs critique"')[1].split("</section>")[0]
        assert "New idea 1" in needs and "New idea 2" in needs
        assert "Critique all 2" in needs
        assert f'action="/ideas/{new_ids[0]}/critique"' in needs

        page = client.get(f"/projects/{project_id}").text
        assert "Critique all 2" in page
        assert page.count('/critique" data-confirm=') >= 3  # one per new idea + "all"

        resp = client.post(
            f"/ideas/{new_ids[0]}/critique",
            data={"next": f"/projects/{project_id}"},
            follow_redirects=False,
        )
        assert resp.headers["location"] == f"/projects/{project_id}"
        evil = client.post(
            f"/ideas/{new_ids[1]}/critique", data={"next": "https://evil.example"},
            follow_redirects=False,
        )  # fmt: skip
        assert evil.headers["location"] == f"/ideas/{new_ids[1]}"  # never an off-site redirect

        client.post(f"/projects/{project_id}/critique", follow_redirects=False)
        deadline = time.monotonic() + 5
        while len(critiqued) < 3:
            assert time.monotonic() < deadline
            time.sleep(0.05)
    assert [new_ids[0]] in critiqued and [new_ids[1]] in critiqued
    assert sorted(new_ids) in [sorted(c or []) for c in critiqued]  # the project-wide run
