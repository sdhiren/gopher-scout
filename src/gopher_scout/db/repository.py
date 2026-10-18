"""Data-access functions. Agents and the UI never write SQL themselves."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from gopher_scout.db.models import AgentRun, AnalysisRun, Event, Idea, Implementation, Project
from gopher_scout.domain.enums import IdeaStatus, ProjectStatus
from gopher_scout.domain.schemas import CritiqueResult, IdeaProposal
from gopher_scout.domain.state import assert_transition

# --------------------------------------------------------------------------- events


def log_event(
    s: Session, entity_type: str, entity_id: int, actor: str, action: str, detail: str | None = None
) -> None:
    s.add(
        Event(
            entity_type=entity_type, entity_id=entity_id, actor=actor, action=action, detail=detail
        )
    )


def events_for(s: Session, entity_type: str, entity_id: int) -> Sequence[Event]:
    return s.scalars(
        select(Event)
        .where(Event.entity_type == entity_type, Event.entity_id == entity_id)
        .order_by(Event.id.desc())
    ).all()


def recent_events(s: Session, limit: int = 20) -> Sequence[Event]:
    return s.scalars(select(Event).order_by(Event.id.desc()).limit(limit)).all()


# --------------------------------------------------------------------------- projects


def upsert_project(s: Session, full_name: str, **fields: Any) -> tuple[Project, bool]:
    """Insert or refresh repo metadata. Never resets status of a known project."""
    project = s.scalar(select(Project).where(Project.full_name == full_name))
    created = project is None
    if project is None:
        project = Project(full_name=full_name)
        s.add(project)
    for key, value in fields.items():
        setattr(project, key, value)
    s.flush()
    return project, created


def get_project(s: Session, project_id: int) -> Project | None:
    return s.get(Project, project_id)


def get_project_by_name(s: Session, full_name: str) -> Project | None:
    return s.scalar(select(Project).where(Project.full_name == full_name))


def list_projects(s: Session, status: ProjectStatus | None = None) -> Sequence[Project]:
    q = (
        select(Project)
        .options(selectinload(Project.ideas))
        .order_by(Project.selection_score.desc())
    )
    if status:
        q = q.where(Project.status == status)
    return s.scalars(q).all()


def set_project_status(
    s: Session, project: Project, status: ProjectStatus, actor: str, reason: str | None = None
) -> None:
    project.status = status
    project.status_reason = reason
    log_event(s, "project", project.id, actor, f"status:{status}", reason)


def add_analysis_run(
    s: Session, project: Project, commit_sha: str, profile: dict[str, Any]
) -> AnalysisRun:
    run = AnalysisRun(project=project, commit_sha=commit_sha, profile=profile)
    s.add(run)
    s.flush()
    return run


# --------------------------------------------------------------------------- ideas


def add_idea(s: Session, project: Project, run: AnalysisRun | None, proposal: IdeaProposal) -> Idea:
    data = proposal.model_dump(mode="json")
    idea = Idea(project=project, analysis_run_id=run.id if run else None, **data)
    s.add(idea)
    s.flush()
    log_event(s, "idea", idea.id, "analyst", "proposed", idea.title)
    return idea


def get_idea(s: Session, idea_id: int) -> Idea | None:
    return s.get(Idea, idea_id)


def list_ideas(
    s: Session, statuses: Sequence[IdeaStatus] | None = None, project_id: int | None = None
) -> Sequence[Idea]:
    q = (
        select(Idea)
        .options(selectinload(Idea.project))
        .order_by(Idea.overall_score.desc().nulls_last(), Idea.id.desc())
    )
    if statuses:
        q = q.where(Idea.status.in_(statuses))
    if project_id:
        q = q.where(Idea.project_id == project_id)
    return s.scalars(q).all()


def transition_idea(
    s: Session, idea: Idea, target: IdeaStatus, actor: str, detail: str | None = None
) -> None:
    assert_transition(idea.status, target)
    previous = idea.status
    idea.status = target
    log_event(s, "idea", idea.id, actor, f"{previous}->{target}", detail)


def apply_critique(
    s: Session, idea: Idea, critique: CritiqueResult, accept_threshold: float
) -> None:
    idea.impact_score = critique.impact_score
    idea.confidence_score = critique.confidence_score
    idea.merge_likelihood = critique.merge_likelihood
    idea.overall_score = critique.overall_score
    idea.critic_reasoning = critique.reasoning
    idea.critic_suggestions = critique.suggested_changes
    idea.duplicate_urls = critique.duplicate_urls
    accepted = (
        critique.verdict == "accept"
        and not critique.duplicate_urls
        and critique.overall_score >= accept_threshold
    )
    target = IdeaStatus.AWAITING_REVIEW if accepted else IdeaStatus.CRITIC_REJECTED
    transition_idea(s, idea, target, "critic", f"score={critique.overall_score}")


# --------------------------------------------------------------------------- implementations


def start_implementation(
    s: Session, idea: Idea, repo_path: str, branch: str, base_sha: str
) -> Implementation:
    impl = Implementation(idea=idea, repo_path=repo_path, branch=branch, base_sha=base_sha)
    s.add(impl)
    s.flush()
    log_event(s, "implementation", impl.id, "implementer", "started", branch)
    return impl


def get_implementation(s: Session, impl_id: int) -> Implementation | None:
    return s.get(Implementation, impl_id)


# --------------------------------------------------------------------------- agent runs / stats


def record_agent_run(
    s: Session,
    agent: str,
    target: str,
    *,
    ok: bool,
    cost_usd: float = 0.0,
    num_turns: int = 0,
    duration_ms: int = 0,
    error: str | None = None,
) -> None:
    s.add(
        AgentRun(
            agent=agent,
            target=target,
            ok=ok,
            cost_usd=cost_usd,
            num_turns=num_turns,
            duration_ms=duration_ms,
            error=error,
        )
    )


@dataclass(frozen=True)
class DashboardStats:
    projects_by_status: dict[str, int]
    ideas_by_status: dict[str, int]
    total_cost_usd: float
    cost_by_agent: dict[str, float]
    approval_rate: float | None


def dashboard_stats(s: Session) -> DashboardStats:
    projects = Counter(
        {
            str(k): v
            for k, v in s.execute(select(Project.status, func.count()).group_by(Project.status))
        }
    )
    ideas = Counter(
        {str(k): v for k, v in s.execute(select(Idea.status, func.count()).group_by(Idea.status))}
    )
    costs = {
        agent: round(cost or 0.0, 2)
        for agent, cost in s.execute(
            select(AgentRun.agent, func.sum(AgentRun.cost_usd)).group_by(AgentRun.agent)
        )
    }
    decided_yes = sum(
        ideas[str(st)]
        for st in (
            IdeaStatus.APPROVED,
            IdeaStatus.IN_PROGRESS,
            IdeaStatus.NEEDS_ATTENTION,
            IdeaStatus.READY_FOR_PR,
            IdeaStatus.PR_OPENED,
            IdeaStatus.MERGED,
            IdeaStatus.CLOSED,
        )
    )
    decided = decided_yes + ideas[str(IdeaStatus.REJECTED)]
    return DashboardStats(
        projects_by_status=dict(projects),
        ideas_by_status=dict(ideas),
        total_cost_usd=round(sum(costs.values()), 2),
        cost_by_agent=costs,
        approval_rate=round(decided_yes / decided, 2) if decided else None,
    )
