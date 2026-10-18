"""Command-line entry point. Every stage is triggered by hand."""

from __future__ import annotations

import asyncio
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from gopher_scout.config import get_settings
from gopher_scout.db import repository as repo
from gopher_scout.db.session import session_scope
from gopher_scout.domain.enums import IdeaStatus
from gopher_scout.logging import configure_logging
from gopher_scout.orchestrator.pipeline import Pipeline

app = typer.Typer(help="Discover Go CLI projects, propose improvements, implement approved ones.")
ideas_app = typer.Typer(help="List and decide on ideas.")
app.add_typer(ideas_app, name="ideas")
console = Console()

IdList = Annotated[list[int] | None, typer.Argument(help="IDs; omit to pick automatically")]


@app.callback()
def _main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    configure_logging(verbose)


def _pipeline() -> Pipeline:
    return Pipeline(get_settings())


@app.command()
def discover() -> None:
    """Scout: search GitHub for candidate Go CLI repositories."""
    result = asyncio.run(_pipeline().discover())
    console.print(f"[green]{result.seen} repos matched, {result.new} new[/]")


@app.command()
def profile(
    project_ids: IdList = None,
    limit: Annotated[int, typer.Option(help="Max projects when no IDs given")] = 5,
    force: Annotated[bool, typer.Option(help="Re-profile even if the commit is unchanged")] = False,
) -> None:
    """Profiler: clone and statically analyse discovered projects."""
    console.print(str(asyncio.run(_pipeline().profile(project_ids, limit, force))))


@app.command()
def analyse(
    project_ids: IdList = None,
    limit: Annotated[int, typer.Option(help="Max projects when no IDs given")] = 3,
) -> None:
    """Analyst: propose improvement ideas for profiled projects (uses Claude)."""
    console.print(str(asyncio.run(_pipeline().analyse(project_ids, limit))))


@app.command()
def critique(idea_ids: IdList = None) -> None:
    """Critic: verify and score proposed ideas (uses Claude)."""
    console.print(str(asyncio.run(_pipeline().critique(idea_ids))))


@app.command()
def research(limit: Annotated[int, typer.Option(help="Projects to take end-to-end")] = 3) -> None:
    """Run discover → profile → analyse → critique in one go."""
    for stage, outcome in asyncio.run(_pipeline().run_research(limit)).items():
        console.print(f"[bold]{stage}[/]: {outcome}")


@app.command()
def implement(idea_id: int) -> None:
    """Implementer + Reviewer: build an approved idea on a local branch (never pushed)."""
    impl_id = asyncio.run(_pipeline().implement(idea_id))
    with session_scope() as s:
        impl = repo.get_implementation(s, impl_id)
        assert impl is not None
        console.print(
            f"[bold]{impl.status}[/] on branch [cyan]{impl.branch}[/] in {impl.repo_path}"
        )
        if impl.commit_sha:
            console.print(
                f"commit {impl.commit_sha[:10]} — review it, then push and open the PR yourself"
            )
        if impl.error:
            console.print(f"[red]{impl.error}[/]")


@app.command()
def projects() -> None:
    """List tracked projects."""
    table = Table("ID", "Repository", "★", "Score", "Status", "Ideas")
    with session_scope() as s:
        for p in repo.list_projects(s):
            table.add_row(
                str(p.id),
                p.full_name,
                str(p.stars),
                f"{p.selection_score:.1f}",
                str(p.status),
                str(len(p.ideas)),
            )
    console.print(table)


@ideas_app.command("list")
def ideas_list(
    status: Annotated[list[IdeaStatus] | None, typer.Option("--status", "-s")] = None,
) -> None:
    """List ideas, best first."""
    table = Table("ID", "Project", "Title", "Category", "Effort", "Score", "Status")
    with session_scope() as s:
        for i in repo.list_ideas(s, status):
            table.add_row(
                str(i.id), i.project.full_name, i.title, str(i.category), str(i.effort),
                f"{i.overall_score:.1f}" if i.overall_score is not None else "-", str(i.status),
            )  # fmt: skip
    console.print(table)


def _decide(idea_id: int, decision: IdeaStatus, notes: str | None) -> None:
    _pipeline().decide(idea_id, decision, notes)
    console.print(f"idea #{idea_id} → [bold]{decision}[/]")


@ideas_app.command()
def approve(
    idea_id: int,
    notes: Annotated[str | None, typer.Option(help="Guidance for the Implementer")] = None,
) -> None:
    _decide(idea_id, IdeaStatus.APPROVED, notes)


@ideas_app.command()
def reject(
    idea_id: int,
    notes: Annotated[str | None, typer.Option(help="Why (feeds future tuning)")] = None,
) -> None:
    _decide(idea_id, IdeaStatus.REJECTED, notes)


@ideas_app.command()
def defer(idea_id: int, notes: str | None = None) -> None:
    _decide(idea_id, IdeaStatus.DEFERRED, notes)


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8420,
    reload: Annotated[bool, typer.Option(help="Auto-reload on code changes")] = False,
) -> None:
    """Start the tracking UI."""
    import uvicorn

    console.print(f"Tracking UI → [link]http://{host}:{port}[/link]")
    uvicorn.run(
        "gopher_scout.web.app:create_app", factory=True, host=host, port=port, reload=reload
    )


if __name__ == "__main__":
    app()
