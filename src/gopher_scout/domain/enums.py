"""Enumerations shared by the database, agents and UI."""

from __future__ import annotations

from enum import StrEnum


class ProjectStatus(StrEnum):
    DISCOVERED = "discovered"
    PROFILED = "profiled"
    ANALYSED = "analysed"
    SKIPPED = "skipped"  # e.g. the repo forbids AI-assisted contributions
    FAILED = "failed"


class IdeaStatus(StrEnum):
    PROPOSED = "proposed"  # written by the Analyst, not yet critiqued
    CRITIC_REJECTED = "critic_rejected"
    AWAITING_REVIEW = "awaiting_review"  # survived the Critic; waiting on you
    APPROVED = "approved"
    REJECTED = "rejected"
    DEFERRED = "deferred"
    IN_PROGRESS = "in_progress"
    NEEDS_ATTENTION = "needs_attention"  # implementation could not pass review
    READY_FOR_PR = "ready_for_pr"  # committed on a local branch; you push it
    PR_OPENED = "pr_opened"
    MERGED = "merged"
    CLOSED = "closed"


class ImplementationStatus(StrEnum):
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class IdeaCategory(StrEnum):
    PERFORMANCE = "performance"
    RELIABILITY = "reliability"
    SECURITY = "security"
    UX = "ux"  # command-line ergonomics: flags, help, errors, completion
    API = "api"
    TESTING = "testing"
    OBSERVABILITY = "observability"
    MAINTAINABILITY = "maintainability"
    DOCS = "docs"


class Effort(StrEnum):
    S = "S"  # < half a day, < ~150 lines
    M = "M"  # 1-2 days
    L = "L"  # multi-day; should start as an issue/discussion


class ContributionStrategy(StrEnum):
    DIRECT_PR = "direct_pr"
    ISSUE_FIRST = "issue_first"


class CriticVerdict(StrEnum):
    ACCEPT = "accept"
    REJECT = "reject"
