"""Implementer agent: edits code on the feature branch to realise an approved idea.

The agent can read, edit and run an allowlisted set of Go commands inside Claude Code's
OS-level sandbox (filesystem writes limited to the repo, network limited to the Go
module proxy). It never commits or pushes; the orchestrator commits under your identity.
"""

from __future__ import annotations

import json
from pathlib import Path

from gopher_scout.agents.base import LLMAgent, load_prompt
from gopher_scout.agents.guards import bash_allowlist_hook, deny_patterns
from gopher_scout.db.models import Idea
from gopher_scout.domain.schemas import ImplementationReport
from gopher_scout.services.sandbox import go_cache_env

TOOLS = ["Read", "Edit", "Write", "Glob", "Grep", "Bash"]

BASH_SANDBOX = {
    "enabled": True,
    "autoAllowBashIfSandboxed": True,
    "allowUnsandboxedCommands": False,
    "network": {"allowedDomains": ["proxy.golang.org", "sum.golang.org", "storage.googleapis.com"]},
}


def build_prompt(idea: Idea, feedback: str | None) -> str:
    spec = {
        "title": idea.title,
        "problem_statement": idea.problem_statement,
        "evidence": idea.evidence,
        "root_cause": idea.root_cause,
        "proposed_solution": idea.proposed_solution,
        "code_sketch": idea.code_sketch,
        "risks": idea.risks,
        "files_to_touch": idea.files_to_touch,
        "acceptance_criteria": idea.acceptance_criteria,
        "critic_suggestions": idea.critic_suggestions,
    }
    parts = ["## Approved idea\n```json\n" + json.dumps(spec, indent=2) + "\n```"]
    if idea.user_notes:
        parts.append("## Notes from the human who approved it (follow these)\n" + idea.user_notes)
    if feedback:
        parts.append(
            "## Reviewer feedback on your previous attempt (address every point)\n" + feedback
        )
    return "\n\n".join(parts)


class ImplementerAgent(LLMAgent):
    name = "implementer"
    max_turns = 150

    async def run(
        self, idea: Idea, repo_path: Path, feedback: str | None = None
    ) -> ImplementationReport:
        return await self.run_structured(
            prompt=build_prompt(idea, feedback),
            output=ImplementationReport,
            cwd=repo_path,
            target=f"idea #{idea.id}",
            tools=TOOLS,
            system_prompt=load_prompt("implementer"),
            hooks=bash_allowlist_hook(),
            disallowed_tools=deny_patterns(),
            sandbox=BASH_SANDBOX,
            env=go_cache_env(repo_path),
        )
