"""SQLAlchemy ORM models — the single source of truth the agents and UI share."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from gopher_scout.domain.enums import (
    ContributionStrategy,
    Effort,
    IdeaCategory,
    IdeaStatus,
    ImplementationStatus,
    ProjectStatus,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _enum(cls: type) -> Enum:
    # Store the readable value ("awaiting_review"), not the member name.
    return Enum(cls, native_enum=False, values_callable=lambda e: [m.value for m in e], length=32)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON, list[Any]: JSON}


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Project(TimestampMixin, Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    html_url: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    stars: Mapped[int] = mapped_column(Integer, default=0)
    forks: Mapped[int] = mapped_column(Integer, default=0)
    open_issues: Mapped[int] = mapped_column(Integer, default=0)
    topics: Mapped[list[Any]] = mapped_column(default=list)
    license: Mapped[str | None] = mapped_column(String(100))
    default_branch: Mapped[str] = mapped_column(String(100), default="main")
    pushed_at: Mapped[datetime | None]
    selection_score: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[ProjectStatus] = mapped_column(
        _enum(ProjectStatus), default=ProjectStatus.DISCOVERED
    )
    status_reason: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(String(500))

    analysis_runs: Mapped[list[AnalysisRun]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="AnalysisRun.id.desc()"
    )
    ideas: Mapped[list[Idea]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Idea.id"
    )

    @property
    def latest_run(self) -> AnalysisRun | None:
        return self.analysis_runs[0] if self.analysis_runs else None


class AnalysisRun(TimestampMixin, Base):
    """One profiling (+ optional analysis) pass over a specific commit."""

    __tablename__ = "analysis_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    commit_sha: Mapped[str] = mapped_column(String(64))
    profile: Mapped[dict[str, Any]] = mapped_column(default=dict)
    analysed: Mapped[bool] = mapped_column(default=False)

    project: Mapped[Project] = relationship(back_populates="analysis_runs")


class Idea(TimestampMixin, Base):
    __tablename__ = "ideas"

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    analysis_run_id: Mapped[int | None] = mapped_column(ForeignKey("analysis_runs.id"))
    status: Mapped[IdeaStatus] = mapped_column(
        _enum(IdeaStatus), default=IdeaStatus.PROPOSED, index=True
    )

    # Analyst output (see domain.schemas.IdeaProposal)
    title: Mapped[str] = mapped_column(String(200))
    category: Mapped[IdeaCategory] = mapped_column(_enum(IdeaCategory))
    summary: Mapped[str] = mapped_column(Text)
    problem_statement: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list[Any]] = mapped_column(default=list)
    root_cause: Mapped[str] = mapped_column(Text)
    proposed_solution: Mapped[str] = mapped_column(Text)
    code_sketch: Mapped[str] = mapped_column(Text, default="")
    alternatives_considered: Mapped[str] = mapped_column(Text, default="")
    benefits: Mapped[list[Any]] = mapped_column(default=list)
    quantified_impact: Mapped[str] = mapped_column(Text, default="")
    risks: Mapped[str] = mapped_column(Text)
    backward_compatible: Mapped[bool] = mapped_column(default=True)
    effort: Mapped[Effort] = mapped_column(_enum(Effort))
    files_to_touch: Mapped[list[Any]] = mapped_column(default=list)
    acceptance_criteria: Mapped[list[Any]] = mapped_column(default=list)
    contribution_strategy: Mapped[ContributionStrategy] = mapped_column(_enum(ContributionStrategy))
    related_issue_urls: Mapped[list[Any]] = mapped_column(default=list)

    # Critic output
    impact_score: Mapped[int | None]
    confidence_score: Mapped[int | None]
    merge_likelihood: Mapped[int | None]
    overall_score: Mapped[float | None]
    critic_reasoning: Mapped[str | None] = mapped_column(Text)
    critic_suggestions: Mapped[str | None] = mapped_column(Text)
    duplicate_urls: Mapped[list[Any]] = mapped_column(default=list)

    # Your input
    user_notes: Mapped[str | None] = mapped_column(Text)
    pr_url: Mapped[str | None] = mapped_column(String(300))

    project: Mapped[Project] = relationship(back_populates="ideas")
    analysis_run: Mapped[AnalysisRun | None] = relationship()
    implementations: Mapped[list[Implementation]] = relationship(
        back_populates="idea", cascade="all, delete-orphan", order_by="Implementation.id.desc()"
    )

    @property
    def latest_implementation(self) -> Implementation | None:
        return self.implementations[0] if self.implementations else None


class Implementation(TimestampMixin, Base):
    __tablename__ = "implementations"

    id: Mapped[int] = mapped_column(primary_key=True)
    idea_id: Mapped[int] = mapped_column(ForeignKey("ideas.id"), index=True)
    status: Mapped[ImplementationStatus] = mapped_column(
        _enum(ImplementationStatus), default=ImplementationStatus.RUNNING
    )
    repo_path: Mapped[str] = mapped_column(String(500))
    branch: Mapped[str] = mapped_column(String(200))
    base_sha: Mapped[str | None] = mapped_column(String(64))
    commit_sha: Mapped[str | None] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str | None] = mapped_column(Text)
    commit_message: Mapped[str | None] = mapped_column(Text)
    diff_stat: Mapped[str | None] = mapped_column(Text)
    review: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(Text)

    idea: Mapped[Idea] = relationship(back_populates="implementations")


class AgentRun(TimestampMixin, Base):
    """Cost and outcome of every agent invocation, for the dashboard."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str] = mapped_column(String(50), index=True)
    target: Mapped[str] = mapped_column(String(200))
    ok: Mapped[bool] = mapped_column(default=True)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    num_turns: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class Event(Base):
    """Append-only audit log: who did what, to which entity, and when."""

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    entity_type: Mapped[str] = mapped_column(String(30))  # project | idea | implementation
    entity_id: Mapped[int] = mapped_column(Integer, index=True)
    actor: Mapped[str] = mapped_column(String(50))  # agent name or "you"
    action: Mapped[str] = mapped_column(String(50))
    detail: Mapped[str | None] = mapped_column(Text)
