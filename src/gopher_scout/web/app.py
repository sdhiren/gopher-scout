"""Tracking UI: dashboard, idea board, idea detail with approve/reject, projects."""

from __future__ import annotations

import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from gopher_scout.config import get_settings
from gopher_scout.db import repository as repo
from gopher_scout.db.session import session_scope
from gopher_scout.domain.enums import IdeaStatus, ProjectStatus
from gopher_scout.domain.state import allowed_transitions
from gopher_scout.orchestrator.pipeline import IMPLEMENTABLE, Pipeline
from gopher_scout.web.jobs import JobManager

HERE = Path(__file__).parent

# Board columns: (title, statuses shown in it)
BOARD: list[tuple[str, tuple[IdeaStatus, ...]]] = [
    ("Needs critique", (IdeaStatus.PROPOSED,)),
    ("Awaiting your review", (IdeaStatus.AWAITING_REVIEW,)),
    ("Approved", (IdeaStatus.APPROVED,)),
    ("In progress", (IdeaStatus.IN_PROGRESS, IdeaStatus.NEEDS_ATTENTION)),
    ("Ready for PR", (IdeaStatus.READY_FOR_PR,)),
    ("Upstream", (IdeaStatus.PR_OPENED, IdeaStatus.MERGED, IdeaStatus.CLOSED)),
    ("Parked", (IdeaStatus.DEFERRED, IdeaStatus.REJECTED)),
    ("Rejected by Critic", (IdeaStatus.CRITIC_REJECTED,)),
]


def _ago(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    secs = int((datetime.now(UTC) - value).total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size:
            return f"{secs // size}{unit} ago"
    return "just now"


def _label(value: object) -> str:
    return str(value).replace("_", " ")


def _hue(value: str) -> int:
    """A stable colour hue per repository, for its avatar."""
    return zlib.crc32(value.encode()) % 360


def create_app() -> FastAPI:
    settings = get_settings()
    pipeline = Pipeline(settings)
    jobs = JobManager()

    app = FastAPI(title="gopher-scout", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["ago"] = _ago
    templates.env.filters["label"] = _label
    templates.env.filters["hue"] = _hue
    templates.env.globals["jobs"] = jobs
    templates.env.globals["max_ideas"] = settings.max_ideas_per_project
    templates.env.globals["budget"] = settings.max_budget_usd_per_run

    def nav_counts() -> dict[str, int]:
        with session_scope() as s:
            return {"awaiting": len(repo.list_ideas(s, [IdeaStatus.AWAITING_REVIEW]))}

    templates.env.globals["nav_counts"] = nav_counts

    def critiquing(idea_id: int, project_id: int) -> bool:
        """Is a critique job running that covers this idea?"""
        return (
            jobs.busy("stage:critique")
            or jobs.busy(f"critique:project:{project_id}")
            or jobs.busy(f"critique:idea:{idea_id}")
        )

    templates.env.globals["critiquing"] = critiquing

    def safe_next(url: str, fallback: str) -> str:
        # Only same-site paths, never an absolute URL from the form.
        return url if url.startswith("/") and not url.startswith("//") else fallback
    # Cache-bust the stylesheet whenever it changes.
    templates.env.globals["css_version"] = int((HERE / "static" / "style.css").stat().st_mtime)

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, ctx)

    def back(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    # ------------------------------------------------------------------ pages

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request) -> HTMLResponse:
        with session_scope() as s:
            return render(
                request,
                "dashboard.html",
                stats=repo.dashboard_stats(s),
                awaiting=repo.list_ideas(s, [IdeaStatus.AWAITING_REVIEW])[:8],
                ready=repo.list_ideas(s, [IdeaStatus.READY_FOR_PR, IdeaStatus.NEEDS_ATTENTION]),
                events=repo.recent_events(s, 15),
            )

    @app.get("/ideas", response_class=HTMLResponse)
    def ideas_board(request: Request, project: int | None = None) -> HTMLResponse:
        with session_scope() as s:
            ideas = repo.list_ideas(s, project_id=project)
            columns = [
                (title, [i for i in ideas if i.status in statuses]) for title, statuses in BOARD
            ]
            return render(
                request,
                "ideas.html",
                columns=columns,
                projects=repo.list_projects(s),
                project_filter=project,
            )

    @app.get("/ideas/{idea_id}", response_class=HTMLResponse)
    def idea_detail(request: Request, idea_id: int) -> HTMLResponse:
        with session_scope() as s:
            idea = repo.get_idea(s, idea_id)
            if idea is None:
                raise HTTPException(404)
            sha = idea.analysis_run.commit_sha if idea.analysis_run else idea.project.default_branch
            return render(
                request,
                "idea_detail.html",
                idea=idea,
                blob_base=f"{idea.project.html_url}/blob/{sha}",
                transitions=allowed_transitions(idea.status),
                implementable=idea.status in IMPLEMENTABLE,
                busy=jobs.busy(idea.project.full_name),
                events=repo.events_for(s, "idea", idea_id),
                S=IdeaStatus,
            )

    @app.get("/projects", response_class=HTMLResponse)
    def projects(request: Request) -> HTMLResponse:
        with session_scope() as s:
            return render(request, "projects.html", projects=repo.list_projects(s))

    @app.get("/projects/{project_id}", response_class=HTMLResponse)
    def project_detail(request: Request, project_id: int) -> HTMLResponse:
        with session_scope() as s:
            project = repo.get_project(s, project_id)
            if project is None:
                raise HTTPException(404)
            run = project.latest_run
            return render(
                request,
                "project_detail.html",
                project=project,
                profile=run.profile if run else None,
                events=repo.events_for(s, "project", project_id),
                P=ProjectStatus,
            )

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request) -> HTMLResponse:
        return render(request, "jobs.html")

    # ------------------------------------------------------------------ actions

    @app.post("/ideas/{idea_id}/decision")
    def decide(
        idea_id: int,
        decision: IdeaStatus = Form(...),
        notes: str = Form(""),
        pr_url: str = Form(""),
    ) -> RedirectResponse:
        try:
            pipeline.decide(idea_id, decision, notes or None, pr_url or None)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return back(f"/ideas/{idea_id}")

    # Job-starting endpoints must be async: they schedule tasks on the server's event loop.
    @app.post("/ideas/{idea_id}/implement")
    async def implement(idea_id: int) -> RedirectResponse:
        with session_scope() as s:
            idea = repo.get_idea(s, idea_id)
            if idea is None:
                raise HTTPException(404)
            key, title = idea.project.full_name, idea.title
        # Already running (double click, re-submitted form): just show progress.
        if not jobs.busy(key):
            jobs.submit(f"Implement #{idea_id}: {title}", key, lambda: pipeline.implement(idea_id))
        return back("/jobs")

    @app.post("/ideas/{idea_id}/critique")
    async def critique_idea(idea_id: int, next: str = Form("")) -> RedirectResponse:
        with session_scope() as s:
            idea = repo.get_idea(s, idea_id)
            if idea is None:
                raise HTTPException(404)
            project_id, title = idea.project_id, idea.title
        if not critiquing(idea_id, project_id):
            jobs.submit(
                f"Critique #{idea_id}: {title}",
                f"critique:idea:{idea_id}",
                lambda: pipeline.critique([idea_id]),
            )
        return back(safe_next(next, f"/ideas/{idea_id}"))

    @app.post("/projects/{project_id}/critique")
    async def critique_project(project_id: int, next: str = Form("")) -> RedirectResponse:
        with session_scope() as s:
            project = repo.get_project(s, project_id)
            if project is None:
                raise HTTPException(404)
            name = project.full_name
            ids = [
                i.id
                for i in project.ideas
                if i.status == IdeaStatus.PROPOSED and not jobs.busy(f"critique:idea:{i.id}")
            ]
        key = f"critique:project:{project_id}"
        if ids and not jobs.busy(key) and not jobs.busy("stage:critique"):
            jobs.submit(f"Critique {len(ids)} ideas in {name}", key, lambda: pipeline.critique(ids))
        return back(safe_next(next, f"/projects/{project_id}"))

    @app.post("/actions/{stage}")
    async def run_stage(stage: str, project_id: int | None = Form(None)) -> RedirectResponse:
        ids = [project_id] if project_id else None
        stages = {
            "discover": ("Discover repositories", lambda: pipeline.discover()),
            "profile": ("Profile projects", lambda: pipeline.profile(ids, force=bool(ids))),
            "analyse": ("Analyse projects", lambda: pipeline.analyse(ids)),
            "critique": ("Critique proposed ideas", lambda: pipeline.critique()),
        }
        if stage not in stages:
            raise HTTPException(404)
        label, factory = stages[stage]
        # Already running (double click, re-submitted form): just show progress.
        if not jobs.busy(f"stage:{stage}"):
            jobs.submit(label, f"stage:{stage}", factory)
        return back("/jobs")

    return app
