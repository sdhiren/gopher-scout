"""Reviewer agent: deterministic quality gate, then an LLM review of the diff.

1. gofmt / build / vet / test run in the sandbox (Docker by default). Test failures that
   also happen on the base commit are reported but not blamed on the change.
2. Only if those pass does an LLM review the diff against the acceptance criteria —
   no point paying for a review of code that does not compile.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from gopher_scout.agents.base import LLMAgent, load_prompt
from gopher_scout.db.models import Idea
from gopher_scout.domain.schemas import CheckResult, LLMReview, ReviewReport
from gopher_scout.services.git import GitRepo
from gopher_scout.services.sandbox import Sandbox

READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]
MAX_DIFF_CHARS = 60_000


def affected_packages(changed: list[str]) -> list[str]:
    dirs = sorted({str(Path(f).parent) for f in changed if f.endswith(".go")})
    return [f"./{d}" if d != "." else "." for d in dirs]


class ReviewerAgent(LLMAgent):
    name = "reviewer"
    max_turns = 40

    def __init__(self, settings, sandbox: Sandbox) -> None:  # type: ignore[no-untyped-def]
        super().__init__(settings)
        self.sandbox = sandbox

    async def run(self, idea: Idea, git: GitRepo, base_sha: str) -> ReviewReport:
        changed = await git.changed_files(base_sha)
        if not changed:
            return ReviewReport(
                checks=[
                    CheckResult(
                        name="diff", command="git diff", ok=False, output="no changes were made"
                    )
                ]
            )

        checks = await self._run_checks(git, base_sha, changed)
        report = ReviewReport(checks=checks)
        if not all(c.ok for c in checks):
            return report

        diff = await git.diff(base_sha)
        report.llm_review = await self._llm_review(idea, git.path, diff)
        return report

    async def _run_checks(
        self, git: GitRepo, base_sha: str, changed: list[str]
    ) -> list[CheckResult]:
        repo = git.path
        go_files = [f for f in changed if f.endswith(".go")]
        pkgs = affected_packages(changed)
        checks: list[CheckResult] = []

        if go_files:
            res = await self.sandbox.run(repo, ["gofmt", "-l", *go_files])
            unformatted = res.stdout.strip()
            checks.append(
                CheckResult(
                    name="gofmt",
                    command="gofmt -l <changed files>",
                    ok=res.ok and not unformatted,
                    output=unformatted or res.output,
                )
            )

        for name, cmd in (
            ("build", ["go", "build", "./..."]),
            ("vet", ["go", "vet", *(pkgs or ["./..."])]),
        ):
            res = await self.sandbox.run(repo, cmd)
            checks.append(
                CheckResult(name=name, command=" ".join(cmd), ok=res.ok, output=res.output[-6000:])
            )
            if not res.ok:
                return checks  # no point testing code that does not build

        if pkgs:
            cmd = ["go", "test", "-count=1", "-timeout=10m", *pkgs]
            res = await self.sandbox.run(repo, cmd)
            ok, output = res.ok, res.output[-8000:]
            if not ok and await self._fails_on_base(git, base_sha, cmd):
                ok, output = True, "Pre-existing failure (also fails on base commit):\n" + output
            checks.append(CheckResult(name="test", command=" ".join(cmd), ok=ok, output=output))
        return checks

    async def _fails_on_base(self, git: GitRepo, base_sha: str, cmd: list[str]) -> bool:
        baseline = git.path.parent / f".{git.path.name}-baseline"
        shutil.rmtree(baseline, ignore_errors=True)
        await git.add_worktree(baseline, base_sha)
        try:
            # Packages that are new in the change do not exist on base; only test those that do.
            existing = [p for p in cmd[4:] if (baseline / p).is_dir()]
            if not existing:
                return False
            res = await self.sandbox.run(baseline, [*cmd[:4], *existing])
            return not res.ok
        finally:
            await git.remove_worktree(baseline)
            shutil.rmtree(baseline, ignore_errors=True)

    async def _llm_review(self, idea: Idea, repo: Path, diff: str) -> LLMReview:
        spec = {
            "title": idea.title,
            "problem_statement": idea.problem_statement,
            "proposed_solution": idea.proposed_solution,
            "acceptance_criteria": idea.acceptance_criteria,
            "risks": idea.risks,
        }
        truncated = len(diff) > MAX_DIFF_CHARS
        prompt = "\n\n".join(
            [
                "## Approved idea\n```json\n" + json.dumps(spec, indent=2) + "\n```",
                "## Diff"
                + (" (truncated; read the files on disk for the rest)" if truncated else "")
                + "\n```diff\n"
                + diff[:MAX_DIFF_CHARS]
                + "\n```",
            ]
        )
        return await self.run_structured(
            prompt=prompt,
            output=LLMReview,
            cwd=repo,
            target=f"idea #{idea.id}",
            tools=READ_ONLY_TOOLS,
            system_prompt=load_prompt("reviewer"),
        )
