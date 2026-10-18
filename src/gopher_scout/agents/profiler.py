"""Profiler agent: clones a repository and gathers hard evidence.

Runs static tooling, collects code metrics, reads contribution guidelines, detects
whether the project restricts AI-assisted contributions, and records maintainer
activity. The Analyst then reasons over facts instead of skimming a whole repo.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from gopher_scout.agents.base import Agent
from gopher_scout.db import repository as repo
from gopher_scout.db.models import Project
from gopher_scout.db.session import session_scope
from gopher_scout.domain.enums import ProjectStatus
from gopher_scout.domain.schemas import RepoProfile, ToolRun
from gopher_scout.services import go_tools
from gopher_scout.services.git import GitRepo
from gopher_scout.services.github import GitHubClient

POLICY_FILES = (
    "CONTRIBUTING.md",
    ".github/CONTRIBUTING.md",
    "docs/CONTRIBUTING.md",
    "AI_POLICY.md",
    ".github/AI_POLICY.md",
    "CODE_OF_CONDUCT.md",
    ".github/pull_request_template.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "README.md",
)
CI_MARKERS = (".github/workflows", ".circleci", ".gitlab-ci.yml", ".travis.yml")

_AI_TERMS = re.compile(
    r"\b(AI|LLMs?|ChatGPT|Copilot|Claude|Cursor|[Gg]enerative|"
    r"(?:AI|machine|[Ll][Ll][Mm])[- ](?:generated|assisted|written))\b"
)
_BAN_TERMS = re.compile(
    r"(not (?:be )?(?:accept|allow|permit|welcome)|prohibit|forbid|\bban(?:ned)?\b|"
    r"\breject|will be closed|do not (?:submit|use|open)|don't (?:submit|use|open)|"
    r"\bno AI\b|must not)",
    re.IGNORECASE,
)


def detect_ai_policy(texts: dict[str, str]) -> tuple[str, str | None]:
    """Classify a repo's stance on AI-assisted contributions from its docs."""
    permissive_hit: str | None = None
    for fname, text in texts.items():
        for sentence in re.split(r"(?<=[.!?])\s+|\n{2,}", text):
            if not _AI_TERMS.search(sentence):
                continue
            snippet = f"{fname}: {' '.join(sentence.split())[:400]}"
            if _BAN_TERMS.search(sentence):
                return "restrictive", snippet
            permissive_hit = permissive_hit or snippet
    if permissive_hit:
        return "permissive", permissive_hit
    return "none_found", None


_HTML_BLOCK = re.compile(r"<(p|div|picture|table|details)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")
_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_EMPTY_LINK = re.compile(r"\[\s*\]\([^)]*\)")  # what is left of a linked badge
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")


def _normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def readme_intro(
    text: str, *, skip: str | None = None, max_paragraphs: int = 3, max_chars: int = 900
) -> str | None:
    """The opening prose of a README: no badges, images, HTML or code, up to the first section.

    ``skip`` is text already shown elsewhere (the GitHub description); a paragraph that
    merely repeats it is dropped.
    """
    skip_key = _normalise(skip) if skip else None
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text = _HTML_BLOCK.sub("", text)
    paragraphs: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if block.startswith("#"):
            if paragraphs:
                break  # first section after the intro
            continue  # the title heading
        block = _MD_IMAGE.sub("", _HTML_TAG.sub("", block))
        block = _MD_LINK.sub(r"\1", _MD_EMPTY_LINK.sub("", block))
        block = " ".join(block.split())
        if len(block) < 20 or block.startswith(("|", ">", "---")):
            continue  # badge rows, tables, quotes, rules
        if skip_key and _normalise(block) == skip_key:
            continue
        paragraphs.append(block)
        if len(paragraphs) >= max_paragraphs or sum(map(len, paragraphs)) >= max_chars:
            break
    intro = "\n\n".join(paragraphs)
    return intro[:max_chars].rstrip() if intro else None


def repo_dir(workspace: Path, full_name: str) -> Path:
    owner, name = full_name.split("/", 1)
    return workspace / owner / name


class ProfilerAgent(Agent):
    name = "profiler"

    async def run(
        self, project_id: int, gh: GitHubClient, *, force: bool = False
    ) -> RepoProfile | None:
        with session_scope() as s:
            project = repo.get_project(s, project_id)
            if project is None:
                raise ValueError(f"unknown project id {project_id}")
            full_name, branch = project.full_name, project.default_branch
            description = project.description
            known_shas = {r.commit_sha for r in project.analysis_runs}

        dest = repo_dir(self.settings.workspace_dir, full_name)
        self.log.info("cloning/updating %s -> %s", full_name, dest)
        git = await GitRepo.clone_or_update(f"https://github.com/{full_name}.git", dest, branch)
        await git.checkout_default(branch)
        sha = await git.head_sha()

        if sha in known_shas and not force:
            self.log.info("%s @ %s already profiled; skipping", full_name, sha[:8])
            return None

        profile = await self._build_profile(full_name, branch, sha, dest, gh, description)

        with session_scope() as s:
            project = repo.get_project(s, project_id)
            assert project is not None
            project.local_path = str(dest)
            repo.add_analysis_run(s, project, sha, profile.model_dump(mode="json"))
            self._decide_status(s, project, profile)
        return profile

    async def _build_profile(
        self,
        full_name: str,
        branch: str,
        sha: str,
        dest: Path,
        gh: GitHubClient,
        description: str | None,
    ) -> RepoProfile:
        go_mod = dest / "go.mod"
        mod = go_tools.parse_go_mod(go_mod.read_text()) if go_mod.exists() else go_tools.GoModInfo()
        metrics = go_tools.collect_metrics(dest)

        tool_runs = []
        packages: list[str] = []
        if go_mod.exists():
            packages = await go_tools.list_packages(dest)
            for tool, runner in (
                ("go vet", go_tools.run_go_vet),
                ("staticcheck", go_tools.run_staticcheck),
                ("govulncheck", go_tools.run_govulncheck),
            ):
                try:
                    result = await runner(dest)
                except OSError as exc:  # e.g. a binary built for the wrong CPU architecture
                    result = ToolRun(tool=tool, available=False, ok=False, error=str(exc))
                self.log.info(
                    "  %s: %s, %d findings",
                    result.tool,
                    "ok" if result.ok else ("n/a" if not result.available else "error"),
                    len(result.findings),
                )
                tool_runs.append(result)

        texts = {
            f: (dest / f).read_text(errors="ignore") for f in POLICY_FILES if (dest / f).is_file()
        }
        policy, policy_evidence = detect_ai_policy(texts)
        contributing = next((texts[f] for f in texts if "CONTRIBUTING" in f), None)

        since = datetime.now(UTC) - timedelta(days=90)
        return RepoProfile(
            full_name=full_name,
            commit_sha=sha,
            default_branch=branch,
            go_version=mod.go_version,
            module_path=mod.module_path,
            direct_dependencies=mod.direct_dependencies,
            go_files=metrics.go_files,
            test_files=metrics.test_files,
            lines_of_code=metrics.lines_of_code,
            packages=packages,
            has_ci=any((dest / m).exists() for m in CI_MARKERS),
            has_golangci_config=any(dest.glob(".golangci.*")),
            readme_intro=(
                readme_intro(texts["README.md"], skip=description) if "README.md" in texts else None
            ),
            has_contributing=contributing is not None,
            contributing_excerpt=contributing[:6000] if contributing else None,
            ai_policy=policy,
            ai_policy_evidence=policy_evidence,
            merged_prs_last_90d=await gh.count_merged_prs(full_name, since),
            open_issues=await gh.list_open_issues(full_name),
            tool_runs=tool_runs,
        )

    def _decide_status(self, s: Session, project: Project, profile: RepoProfile) -> None:
        if profile.module_path is None:
            repo.set_project_status(
                s, project, ProjectStatus.SKIPPED, self.name, "no go.mod at repo root"
            )
        elif profile.ai_policy == "restrictive":
            repo.set_project_status(
                s,
                project,
                ProjectStatus.SKIPPED,
                self.name,
                f"repository restricts AI-assisted contributions — {profile.ai_policy_evidence}",
            )
        elif profile.merged_prs_last_90d == 0:
            repo.set_project_status(
                s, project, ProjectStatus.SKIPPED, self.name, "no PRs merged in the last 90 days"
            )
        else:
            repo.set_project_status(
                s, project, ProjectStatus.PROFILED, self.name, f"@ {profile.commit_sha[:8]}"
            )
