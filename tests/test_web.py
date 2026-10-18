from __future__ import annotations

from fastapi.testclient import TestClient

from gopher_scout.config import Settings
from gopher_scout.db import repository as repo
from gopher_scout.db.session import session_scope
from gopher_scout.domain.enums import IdeaStatus
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
