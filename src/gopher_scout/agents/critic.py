"""Critic agent: verifies, de-duplicates and scores each proposed idea."""

from __future__ import annotations

import json
import re
from pathlib import Path

from gopher_scout.agents.base import LLMAgent, load_prompt
from gopher_scout.db import repository as repo
from gopher_scout.db.models import Idea
from gopher_scout.db.session import session_scope
from gopher_scout.domain.schemas import CritiqueResult, IssueSummary
from gopher_scout.services.github import GitHubClient, GitHubError

READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]
ACCEPT_THRESHOLD = 5.0

# fmt: off
_STOPWORDS = frozenset((
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "into", "is",
    "it", "of", "on", "or", "the", "to", "with", "when", "without", "add", "use", "make",
    "avoid", "ensure", "improve", "fix", "support", "handle", "instead",
))
# fmt: on


def search_keywords(title: str, max_words: int = 4) -> str:
    words = [
        w for w in re.findall(r"[A-Za-z][A-Za-z0-9_.-]{2,}", title) if w.lower() not in _STOPWORDS
    ]
    return " ".join(words[:max_words])


def idea_brief(idea: Idea) -> dict[str, object]:
    return {
        "title": idea.title,
        "category": str(idea.category),
        "summary": idea.summary,
        "problem_statement": idea.problem_statement,
        "evidence": idea.evidence,
        "root_cause": idea.root_cause,
        "proposed_solution": idea.proposed_solution,
        "code_sketch": idea.code_sketch,
        "benefits": idea.benefits,
        "risks": idea.risks,
        "effort": str(idea.effort),
        "acceptance_criteria": idea.acceptance_criteria,
        "related_issue_urls": idea.related_issue_urls,
    }


class CriticAgent(LLMAgent):
    name = "critic"
    max_turns = 40

    async def run(self, idea_id: int, gh: GitHubClient) -> CritiqueResult:
        with session_scope() as s:
            idea = repo.get_idea(s, idea_id)
            if idea is None:
                raise ValueError(f"unknown idea {idea_id}")
            brief = idea_brief(idea)
            full_name, path = idea.project.full_name, idea.project.local_path
            contributing = (
                idea.project.latest_run.profile.get("contributing_excerpt")
                if idea.project.latest_run
                else None
            )
        if path is None:
            raise ValueError(f"project for idea {idea_id} has no local clone")

        related: list[IssueSummary] = []
        try:
            related = await gh.search_related(full_name, search_keywords(str(brief["title"])))
        except GitHubError as exc:
            self.log.warning("related-issue search failed: %s", exc)

        prompt = "\n\n".join(
            [
                "## Proposal under review\n```json\n" + json.dumps(brief, indent=2) + "\n```",
                "## Possibly related issues/PRs (open and closed)\n```json\n"
                + json.dumps([r.model_dump() for r in related], indent=1)
                + "\n```",
                "## Contribution guide (excerpt)\n" + (contributing or "(none found)"),
            ]
        )
        critique = await self.run_structured(
            prompt=prompt,
            output=CritiqueResult,
            cwd=Path(path),
            target=f"idea #{idea_id}",
            tools=READ_ONLY_TOOLS,
            system_prompt=load_prompt("critic"),
        )

        with session_scope() as s:
            idea = repo.get_idea(s, idea_id)
            assert idea is not None
            repo.apply_critique(s, idea, critique, ACCEPT_THRESHOLD)
        return critique
