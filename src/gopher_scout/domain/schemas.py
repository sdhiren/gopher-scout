"""Typed contracts between agents.

The LLM agents return these models as structured output (their JSON schema is handed to
the Agent SDK), and deterministic agents produce them directly. Constraints are expressed
as validators rather than JSON-schema keywords, so the schema sent to the model stays
simple while Python still rejects bad data.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from gopher_scout.domain.enums import (
    ContributionStrategy,
    CriticVerdict,
    Effort,
    IdeaCategory,
)


def _require_non_empty(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be empty")
    return value.strip()


def _clamp_score(value: int) -> int:
    return max(0, min(10, value))


# --------------------------------------------------------------------------- profiler


class ToolFinding(BaseModel):
    tool: str
    file: str
    line: int | None = None
    message: str


class ToolRun(BaseModel):
    tool: str
    available: bool
    ok: bool
    findings: list[ToolFinding] = Field(default_factory=list)
    error: str | None = None


class IssueSummary(BaseModel):
    number: int
    title: str
    url: str
    labels: list[str] = Field(default_factory=list)
    is_pr: bool = False
    state: str = "open"


class RepoProfile(BaseModel):
    """Hard evidence gathered before any LLM looks at the code."""

    full_name: str
    commit_sha: str
    default_branch: str
    go_version: str | None = None
    module_path: str | None = None
    direct_dependencies: int = 0
    go_files: int = 0
    test_files: int = 0
    lines_of_code: int = 0
    packages: list[str] = Field(default_factory=list)
    has_ci: bool = False
    has_golangci_config: bool = False
    has_contributing: bool = False
    contributing_excerpt: str | None = None
    ai_policy: str = "none_found"  # none_found | restrictive | permissive
    ai_policy_evidence: str | None = None
    merged_prs_last_90d: int | None = None
    open_issues: list[IssueSummary] = Field(default_factory=list)
    tool_runs: list[ToolRun] = Field(default_factory=list)

    def findings_digest(self, limit: int = 60) -> list[dict[str, Any]]:
        """A compact, de-duplicated slice of tool findings for the Analyst prompt."""
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for run in self.tool_runs:
            for f in run.findings:
                key = (run.tool, f.message)
                if key in seen:
                    continue
                seen.add(key)
                out.append({"tool": run.tool, "file": f.file, "line": f.line, "msg": f.message})
                if len(out) >= limit:
                    return out
        return out


# --------------------------------------------------------------------------- analyst


class Evidence(BaseModel):
    file: str = Field(description="Repository-relative path, e.g. cmd/root.go")
    line_start: int | None = None
    line_end: int | None = None
    snippet: str = Field(default="", description="The few relevant lines, verbatim")
    explanation: str = Field(description="Why this code demonstrates the problem")


class IdeaProposal(BaseModel):
    title: str = Field(description="Imperative, specific, <= 80 chars")
    category: IdeaCategory
    summary: str = Field(description="One sentence a maintainer would read in a PR list")
    problem_statement: str = Field(
        description="What is wrong today, who is affected and when, in plain language"
    )
    evidence: list[Evidence] = Field(description="Concrete code locations; at least one")
    root_cause: str
    proposed_solution: str = Field(description="The approach, step by step")
    code_sketch: str = Field(default="", description="Short Go sketch of the key change")
    alternatives_considered: str = Field(default="")
    benefits: list[str] = Field(description="Concrete outcomes for users or maintainers")
    quantified_impact: str = Field(
        default="", description="Numbers where possible, or how you would measure"
    )
    risks: str = Field(description="Behaviour changes, compatibility, edge cases")
    backward_compatible: bool
    effort: Effort
    files_to_touch: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(
        description="Checkable statements: tests, benchmarks or observable behaviour"
    )
    contribution_strategy: ContributionStrategy
    related_issue_urls: list[str] = Field(default_factory=list)

    _non_empty = field_validator(
        "title", "summary", "problem_statement", "root_cause", "proposed_solution", "risks"
    )(_require_non_empty)

    @field_validator("evidence")
    @classmethod
    def _needs_evidence(cls, v: list[Evidence]) -> list[Evidence]:
        if not v:
            raise ValueError("an idea without evidence is not an idea")
        return v

    @field_validator("benefits", "acceptance_criteria")
    @classmethod
    def _needs_items(cls, v: list[str]) -> list[str]:
        items = [s.strip() for s in v if s.strip()]
        if not items:
            raise ValueError("at least one item is required")
        return items


class AnalysisResult(BaseModel):
    project_summary: str = Field(
        description="2-4 sentences: what the tool does and the state of its codebase"
    )
    ideas: list[IdeaProposal]


# --------------------------------------------------------------------------- critic


class CritiqueResult(BaseModel):
    verdict: CriticVerdict
    impact_score: int = Field(description="0-10: value to users/maintainers if merged")
    confidence_score: int = Field(description="0-10: how sure the problem is real")
    merge_likelihood: int = Field(description="0-10: chance maintainers accept it")
    duplicate_urls: list[str] = Field(
        default_factory=list, description="Existing issues/PRs covering the same thing"
    )
    reasoning: str = Field(description="Why this verdict; cite what you checked")
    suggested_changes: str = Field(
        default="", description="How the proposal should be sharpened, if at all"
    )

    _clamp = field_validator("impact_score", "confidence_score", "merge_likelihood")(_clamp_score)

    @property
    def overall_score(self) -> float:
        # Confidence gates everything: a high-impact idea that is probably wrong is worthless.
        return round(
            (0.45 * self.impact_score + 0.25 * self.merge_likelihood) * (self.confidence_score / 10)
            + 0.3 * self.confidence_score,
            2,
        )


# --------------------------------------------------------------------------- implementer / reviewer


class ImplementationReport(BaseModel):
    summary: str = Field(description="What was changed and why, for the PR description")
    commit_subject: str = Field(description="Conventional-commit subject, <= 72 chars")
    commit_body: str = Field(default="", description="Wrapped at 72 columns; no trailers")
    files_changed: list[str] = Field(default_factory=list)
    tests_added: list[str] = Field(default_factory=list)
    verification: str = Field(description="Commands you ran and their outcome")
    open_questions: str = Field(default="")

    _non_empty = field_validator("summary", "commit_subject")(_require_non_empty)


class CheckResult(BaseModel):
    name: str
    command: str
    ok: bool
    output: str = ""


class ReviewFinding(BaseModel):
    severity: str = Field(description="blocker | major | minor")
    file: str = ""
    message: str


class LLMReview(BaseModel):
    approve: bool
    criteria_met: list[str] = Field(default_factory=list)
    criteria_missed: list[str] = Field(default_factory=list)
    findings: list[ReviewFinding] = Field(default_factory=list)
    summary: str


class ReviewReport(BaseModel):
    checks: list[CheckResult]
    llm_review: LLMReview | None = None

    @property
    def passed(self) -> bool:
        checks_ok = all(c.ok for c in self.checks)
        return checks_ok and (self.llm_review is None or self.llm_review.approve)

    def feedback_for_implementer(self) -> str:
        lines: list[str] = []
        for c in self.checks:
            if not c.ok:
                lines.append(f"Check `{c.name}` failed (`{c.command}`):\n{c.output[-3000:]}")
        if self.llm_review and not self.llm_review.approve:
            lines.append(f"Reviewer summary: {self.llm_review.summary}")
            lines.extend(f"- missed criterion: {m}" for m in self.llm_review.criteria_missed)
            lines.extend(
                f"- [{f.severity}] {f.file}: {f.message}" for f in self.llm_review.findings
            )
        return "\n\n".join(lines)
