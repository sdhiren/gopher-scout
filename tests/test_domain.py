from __future__ import annotations

import itertools

import pytest
from pydantic import ValidationError

from gopher_scout.domain.enums import CriticVerdict, IdeaStatus
from gopher_scout.domain.schemas import (
    AnalysisResult,
    CheckResult,
    CritiqueResult,
    LLMReview,
    ReviewReport,
)
from gopher_scout.domain.state import InvalidTransitionError, assert_transition
from tests.conftest import make_proposal


class TestStateMachine:
    def test_happy_path(self) -> None:
        path = [
            IdeaStatus.PROPOSED,
            IdeaStatus.AWAITING_REVIEW,
            IdeaStatus.APPROVED,
            IdeaStatus.IN_PROGRESS,
            IdeaStatus.READY_FOR_PR,
            IdeaStatus.PR_OPENED,
            IdeaStatus.MERGED,
        ]
        for current, target in itertools.pairwise(path):
            assert_transition(current, target)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            (IdeaStatus.PROPOSED, IdeaStatus.IN_PROGRESS),  # must be approved first
            (IdeaStatus.AWAITING_REVIEW, IdeaStatus.IN_PROGRESS),
            (IdeaStatus.CRITIC_REJECTED, IdeaStatus.APPROVED),  # overrule goes via review
            (IdeaStatus.MERGED, IdeaStatus.REJECTED),
        ],
    )
    def test_illegal_transitions(self, current: IdeaStatus, target: IdeaStatus) -> None:
        with pytest.raises(InvalidTransitionError):
            assert_transition(current, target)


class TestSchemas:
    def test_idea_requires_evidence(self) -> None:
        with pytest.raises(ValidationError, match="evidence"):
            make_proposal(evidence=[])

    def test_idea_requires_acceptance_criteria(self) -> None:
        with pytest.raises(ValidationError):
            make_proposal(acceptance_criteria=["  "])

    def test_json_schema_is_generated_for_agents(self) -> None:
        schema = AnalysisResult.model_json_schema()
        assert "ideas" in schema["properties"]

    def test_critique_scores_are_clamped_and_combined(self) -> None:
        c = CritiqueResult(
            verdict=CriticVerdict.ACCEPT,
            impact_score=14,
            confidence_score=10,
            merge_likelihood=10,
            reasoning="checked",
        )
        assert c.impact_score == 10
        assert c.overall_score == 10.0

    def test_low_confidence_drags_score_down(self) -> None:
        sure = CritiqueResult(verdict="accept", impact_score=8, confidence_score=9,
                              merge_likelihood=7, reasoning="x")  # fmt: skip
        unsure = sure.model_copy(update={"confidence_score": 3})
        assert unsure.overall_score < sure.overall_score / 2

    def test_review_report_passes_only_when_everything_passes(self) -> None:
        ok = CheckResult(name="build", command="go build", ok=True)
        bad = CheckResult(name="test", command="go test", ok=False, output="FAIL")
        assert ReviewReport(checks=[ok]).passed
        assert not ReviewReport(checks=[ok, bad]).passed
        rejected = ReviewReport(
            checks=[ok], llm_review=LLMReview(approve=False, summary="missing test")
        )
        assert not rejected.passed
        assert "missing test" in rejected.feedback_for_implementer()
