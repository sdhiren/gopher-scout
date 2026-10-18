"""Analyst agent: reads the code + profile and proposes evidence-backed improvements."""

from __future__ import annotations

import json
from pathlib import Path

from gopher_scout.agents.base import LLMAgent, load_prompt
from gopher_scout.db import repository as repo
from gopher_scout.db.session import session_scope
from gopher_scout.domain.enums import ProjectStatus
from gopher_scout.domain.schemas import AnalysisResult, RepoProfile

READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]


def build_prompt(profile: RepoProfile, description: str | None, max_ideas: int) -> str:
    facts = {
        "repository": profile.full_name,
        "description": description,
        "commit": profile.commit_sha,
        "go_version": profile.go_version,
        "module": profile.module_path,
        "direct_dependencies": profile.direct_dependencies,
        "go_files": profile.go_files,
        "test_files": profile.test_files,
        "lines_of_code": profile.lines_of_code,
        "packages": profile.packages[:80],
        "has_ci": profile.has_ci,
        "has_golangci_config": profile.has_golangci_config,
        "merged_prs_last_90d": profile.merged_prs_last_90d,
        "tools": [
            {"tool": t.tool, "available": t.available, "ok": t.ok, "findings": len(t.findings)}
            for t in profile.tool_runs
        ],
    }
    issues = [
        i.model_dump(include={"number", "title", "url", "labels", "is_pr"})
        for i in profile.open_issues
    ]
    sections = [
        "## Repository facts\n```json\n" + json.dumps(facts, indent=2) + "\n```",
        "## Static-analysis leads (verify before using)\n```json\n"
        + json.dumps(profile.findings_digest(), indent=1)
        + "\n```",
        "## Open issues and PRs (most recently updated)\n```json\n"
        + json.dumps(issues, indent=1)
        + "\n```",
    ]
    if profile.contributing_excerpt:
        sections.append("## Contribution guide (excerpt)\n" + profile.contributing_excerpt)
    sections.append(
        f"## Task\nPropose at most {max_ideas} improvements, best first. "
        "Fewer, stronger ideas beat a long list."
    )
    return "\n\n".join(sections)


class AnalystAgent(LLMAgent):
    name = "analyst"
    max_turns = 80

    async def run(self, project_id: int) -> int:
        """Analyse the latest profile of a project. Returns the number of ideas stored."""
        with session_scope() as s:
            project = repo.get_project(s, project_id)
            if project is None or project.latest_run is None or project.local_path is None:
                raise ValueError(f"project {project_id} has not been profiled")
            profile = RepoProfile.model_validate(project.latest_run.profile)
            description, full_name, path = (
                project.description,
                project.full_name,
                project.local_path,
            )
            run_id = project.latest_run.id

        result = await self.run_structured(
            prompt=build_prompt(profile, description, self.settings.max_ideas_per_project),
            output=AnalysisResult,
            cwd=Path(path),
            target=full_name,
            tools=READ_ONLY_TOOLS,
            system_prompt=load_prompt("analyst"),
        )

        with session_scope() as s:
            project = repo.get_project(s, project_id)
            assert project is not None
            run = next(r for r in project.analysis_runs if r.id == run_id)
            for proposal in result.ideas[: self.settings.max_ideas_per_project]:
                repo.add_idea(s, project, run, proposal)
            run.analysed = True
            project.summary = result.project_summary
            repo.set_project_status(
                s, project, ProjectStatus.ANALYSED, self.name, f"{len(result.ideas)} ideas"
            )
        return len(result.ideas)
