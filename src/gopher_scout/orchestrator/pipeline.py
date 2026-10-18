"""Orchestrator: moves projects and ideas through the pipeline.

Agents never call each other. The pipeline reads state from the database, invokes one
agent, and writes the result back, so any stage can be re-run, inspected or replaced
on its own. Every stage is triggered manually (CLI or UI).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from gopher_scout.agents.analyst import AnalystAgent
from gopher_scout.agents.critic import CriticAgent
from gopher_scout.agents.implementer import ImplementerAgent
from gopher_scout.agents.profiler import ProfilerAgent
from gopher_scout.agents.reviewer import ReviewerAgent
from gopher_scout.agents.scout import ScoutAgent, ScoutResult
from gopher_scout.config import Settings
from gopher_scout.db import repository as repo
from gopher_scout.db.session import init_db, session_scope
from gopher_scout.domain.enums import IdeaStatus, ImplementationStatus, ProjectStatus
from gopher_scout.domain.schemas import ReviewReport
from gopher_scout.domain.state import USER_DECISIONS
from gopher_scout.services.git import GitRepo, slugify
from gopher_scout.services.github import GitHubClient
from gopher_scout.services.sandbox import Sandbox, resolve_mode

log = logging.getLogger(__name__)

IMPLEMENTABLE = (IdeaStatus.APPROVED, IdeaStatus.NEEDS_ATTENTION, IdeaStatus.READY_FOR_PR)


class WorkspaceDirtyError(RuntimeError):
    pass


@dataclass
class StageReport:
    succeeded: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{len(self.succeeded)} succeeded, {len(self.failed)} failed"


class Pipeline:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        settings.ensure_dirs()
        init_db()

    def _github(self) -> GitHubClient:
        return GitHubClient(self.settings.resolve_github_token())

    # ------------------------------------------------------------------ discovery & analysis

    async def discover(self) -> ScoutResult:
        async with self._github() as gh:
            return await ScoutAgent(self.settings).run(gh)

    async def profile(
        self, project_ids: Sequence[int] | None = None, limit: int = 5, force: bool = False
    ) -> StageReport:
        ids = project_ids or self._project_ids(ProjectStatus.DISCOVERED, limit)
        report = StageReport()
        agent = ProfilerAgent(self.settings)
        async with self._github() as gh:
            for pid in ids:
                try:
                    await agent.run(pid, gh, force=force)
                    report.succeeded.append(str(pid))
                except Exception as exc:
                    log.exception("profiling project %s failed", pid)
                    self._mark_project_failed(pid, f"profiling failed: {exc}")
                    report.failed.append(str(pid))
        return report

    async def analyse(
        self, project_ids: Sequence[int] | None = None, limit: int = 3
    ) -> StageReport:
        ids = project_ids or self._project_ids(ProjectStatus.PROFILED, limit)
        report = StageReport()
        agent = AnalystAgent(self.settings)
        for pid in ids:
            try:
                n = await agent.run(pid)
                report.succeeded.append(f"{pid} ({n} ideas)")
            except Exception:
                log.exception("analysing project %s failed", pid)
                report.failed.append(str(pid))
        return report

    async def critique(self, idea_ids: Sequence[int] | None = None) -> StageReport:
        """Critique the given ideas (default: every proposed idea). Only `proposed` ideas
        are critiqued; anything else was already decided and is skipped."""
        with session_scope() as s:
            proposed = {i.id for i in repo.list_ideas(s, [IdeaStatus.PROPOSED])}
        if idea_ids:
            skipped = [i for i in idea_ids if i not in proposed]
            if skipped:
                log.info("skipping ideas that are not awaiting critique: %s", skipped)
            idea_ids = [i for i in idea_ids if i in proposed]
        else:
            idea_ids = sorted(proposed)
        report = StageReport()
        agent = CriticAgent(self.settings)
        async with self._github() as gh:
            for iid in idea_ids:
                # Another job may have critiqued it since this one started.
                with session_scope() as s:
                    current = repo.get_idea(s, iid)
                    if current is None or current.status != IdeaStatus.PROPOSED:
                        continue
                try:
                    result = await agent.run(iid, gh)
                    report.succeeded.append(f"#{iid} {result.verdict} ({result.overall_score})")
                except Exception:
                    log.exception("critiquing idea %s failed", iid)
                    report.failed.append(str(iid))
        return report

    async def run_research(self, limit: int = 3) -> dict[str, str]:
        """discover → profile → analyse → critique, for up to `limit` new projects."""
        scout = await self.discover()
        profiled = await self.profile(limit=limit)
        analysed = await self.analyse(limit=limit)
        critiqued = await self.critique()
        return {
            "discover": f"{scout.seen} matched, {scout.new} new",
            "profile": str(profiled),
            "analyse": str(analysed),
            "critique": str(critiqued),
        }

    # ------------------------------------------------------------------ your decisions

    def decide(
        self,
        idea_id: int,
        decision: IdeaStatus,
        notes: str | None = None,
        pr_url: str | None = None,
    ) -> None:
        if decision not in USER_DECISIONS:
            raise ValueError(f"'{decision}' is not a decision you can make by hand")
        with session_scope() as s:
            idea = repo.get_idea(s, idea_id)
            if idea is None:
                raise ValueError(f"unknown idea {idea_id}")
            if notes is not None:
                idea.user_notes = notes.strip() or None
            if pr_url:
                idea.pr_url = pr_url
            repo.transition_idea(s, idea, decision, "you", notes)

    # ------------------------------------------------------------------ implementation

    async def implement(self, idea_id: int) -> int:
        """Implement an approved idea on a fresh local branch. Returns the implementation id."""
        with session_scope() as s:
            idea = repo.get_idea(s, idea_id)
            if idea is None:
                raise ValueError(f"unknown idea {idea_id}")
            if idea.status not in IMPLEMENTABLE:
                raise ValueError(f"idea #{idea_id} is '{idea.status}'; approve it first")
            project = idea.project
            if not project.local_path:
                raise ValueError(f"{project.full_name} has no local clone; profile it first")
            path, full_name, default_branch = (
                Path(project.local_path),
                project.full_name,
                project.default_branch,
            )
            attempt_no = len(idea.implementations) + 1
            branch = f"gs/{idea.id}-{slugify(idea.title)}" + (
                f"-v{attempt_no}" if attempt_no > 1 else ""
            )

        git = await GitRepo.clone_or_update(
            f"https://github.com/{full_name}.git", path, default_branch
        )
        if await git.is_dirty():
            raise WorkspaceDirtyError(f"{path} has uncommitted changes; commit or stash them first")
        await git.checkout_default(default_branch)
        base_sha = await git.head_sha()
        await git.create_branch(branch, base_sha)

        with session_scope() as s:
            idea = repo.get_idea(s, idea_id)
            assert idea is not None
            repo.transition_idea(s, idea, IdeaStatus.IN_PROGRESS, "implementer", branch)
            impl_id = repo.start_implementation(s, idea, str(path), branch, base_sha).id

        try:
            await self._implement_loop(idea_id, impl_id, git, base_sha)
        except Exception as exc:
            log.exception("implementation of idea %s failed", idea_id)
            with session_scope() as s:
                impl = repo.get_implementation(s, impl_id)
                idea = repo.get_idea(s, idea_id)
                assert impl is not None and idea is not None
                impl.status = ImplementationStatus.ERROR
                impl.error = str(exc)[:4000]
                repo.transition_idea(s, idea, IdeaStatus.NEEDS_ATTENTION, "implementer", "error")
        return impl_id

    async def _implement_loop(
        self, idea_id: int, impl_id: int, git: GitRepo, base_sha: str
    ) -> None:
        implementer = ImplementerAgent(self.settings)
        sandbox = Sandbox(
            await resolve_mode(self.settings.sandbox),
            self.settings.docker_image,
            self.settings.command_timeout_s,
        )
        reviewer = ReviewerAgent(self.settings, sandbox)
        log.info("review sandbox: %s", sandbox.mode)

        with session_scope() as s:
            idea = repo.get_idea(s, idea_id)
            assert idea is not None

        feedback: str | None = None
        review: ReviewReport | None = None
        report = None
        attempts = 0
        for attempts in range(1, self.settings.max_implementation_attempts + 1):
            log.info("idea #%s: implementation attempt %d", idea_id, attempts)
            report = await implementer.run(idea, git.path, feedback)
            review = await reviewer.run(idea, git, base_sha)
            if review.passed:
                break
            feedback = review.feedback_for_implementer()
            log.info("idea #%s: review failed, retrying with feedback", idea_id)

        assert report is not None and review is not None
        if not await git.push_disabled():  # paranoia: the agent must not have re-enabled pushes
            await git.harden()

        commit_sha = None
        diff_stat = await git.diff_stat(base_sha)
        if await git.is_dirty():
            commit_sha = await git.commit_all(f"{report.commit_subject}\n\n{report.commit_body}")

        with session_scope() as s:
            impl = repo.get_implementation(s, impl_id)
            idea = repo.get_idea(s, idea_id)
            assert impl is not None and idea is not None
            impl.attempts = attempts
            impl.summary = report.summary
            impl.commit_message = f"{report.commit_subject}\n\n{report.commit_body}".strip()
            impl.commit_sha = commit_sha
            impl.diff_stat = diff_stat
            impl.review = {
                **review.model_dump(mode="json"),
                "passed": review.passed,
                "report": report.model_dump(mode="json"),
            }
            passed = review.passed and commit_sha is not None
            impl.status = ImplementationStatus.PASSED if passed else ImplementationStatus.FAILED
            target = IdeaStatus.READY_FOR_PR if passed else IdeaStatus.NEEDS_ATTENTION
            repo.transition_idea(
                s,
                idea,
                target,
                "reviewer",
                f"{impl.branch} @ {commit_sha[:8] if commit_sha else '-'}",
            )

    # ------------------------------------------------------------------ helpers

    def _project_ids(self, status: ProjectStatus, limit: int) -> list[int]:
        with session_scope() as s:
            return [p.id for p in repo.list_projects(s, status)[:limit]]

    def _mark_project_failed(self, project_id: int, reason: str) -> None:
        with session_scope() as s:
            project = repo.get_project(s, project_id)
            if project:
                repo.set_project_status(s, project, ProjectStatus.FAILED, "profiler", reason[:2000])
