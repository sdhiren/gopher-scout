"""Idea lifecycle state machine.

Every status change goes through :func:`assert_transition`, so an agent bug can never
silently move an idea somewhere it should not be (e.g. straight from ``proposed`` to
``in_progress`` without your approval).
"""

from __future__ import annotations

from gopher_scout.domain.enums import IdeaStatus as S

_TRANSITIONS: dict[S, frozenset[S]] = {
    S.PROPOSED: frozenset({S.AWAITING_REVIEW, S.CRITIC_REJECTED}),
    S.CRITIC_REJECTED: frozenset({S.AWAITING_REVIEW}),  # you may overrule the Critic
    S.AWAITING_REVIEW: frozenset({S.APPROVED, S.REJECTED, S.DEFERRED}),
    S.DEFERRED: frozenset({S.APPROVED, S.REJECTED, S.AWAITING_REVIEW}),
    S.REJECTED: frozenset({S.AWAITING_REVIEW}),
    S.APPROVED: frozenset({S.IN_PROGRESS, S.REJECTED, S.DEFERRED}),
    S.IN_PROGRESS: frozenset({S.READY_FOR_PR, S.NEEDS_ATTENTION}),
    S.NEEDS_ATTENTION: frozenset({S.IN_PROGRESS, S.READY_FOR_PR, S.REJECTED}),
    S.READY_FOR_PR: frozenset({S.PR_OPENED, S.IN_PROGRESS, S.REJECTED}),
    S.PR_OPENED: frozenset({S.MERGED, S.CLOSED}),
    S.MERGED: frozenset(),
    S.CLOSED: frozenset({S.AWAITING_REVIEW}),
}

# Statuses you can set by hand from the UI/CLI.
USER_DECISIONS: frozenset[S] = frozenset(
    {S.APPROVED, S.REJECTED, S.DEFERRED, S.AWAITING_REVIEW, S.PR_OPENED, S.MERGED, S.CLOSED}
)


class InvalidTransitionError(ValueError):
    def __init__(self, current: S, target: S) -> None:
        super().__init__(f"cannot move idea from '{current}' to '{target}'")
        self.current = current
        self.target = target


def allowed_transitions(current: S) -> frozenset[S]:
    return _TRANSITIONS[current]


def assert_transition(current: S, target: S) -> None:
    if target not in _TRANSITIONS[current]:
        raise InvalidTransitionError(current, target)
