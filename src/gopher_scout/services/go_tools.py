"""Static analysis of a Go module: metrics plus vet/staticcheck/govulncheck findings.

These tools compile code but never execute it (no tests, no ``go generate``), so they
run directly on the host. Optional tools that are not installed are reported as
unavailable instead of failing the profile.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from gopher_scout.domain.schemas import ToolFinding, ToolRun
from gopher_scout.services.process import run
from gopher_scout.services.sandbox import go_cache_env

_SKIP_DIRS = {"vendor", "testdata", ".git", ".gs-cache", "node_modules"}
_POS_RE = re.compile(r"^(?P<file>[^\s:][^:]*\.go):(?P<line>\d+)(?::\d+)?:\s*(?P<msg>.+)$")
_VULN_RE = re.compile(r"^Vulnerability #\d+:\s*(?P<id>GO-\d{4}-\d+)\s*$")
MAX_FINDINGS_PER_TOOL = 200


@dataclass
class GoModInfo:
    module_path: str | None = None
    go_version: str | None = None
    direct_dependencies: int = 0


@dataclass
class CodeMetrics:
    go_files: int = 0
    test_files: int = 0
    lines_of_code: int = 0
    packages: list[str] = field(default_factory=list)


def parse_go_mod(text: str) -> GoModInfo:
    info = GoModInfo()
    in_require = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("module "):
            info.module_path = line.split()[1]
        elif line.startswith("go "):
            info.go_version = line.split()[1]
        elif line == "require (":
            in_require = True
        elif in_require and line == ")":
            in_require = False
        elif (
            (in_require or line.startswith("require "))
            and line
            and not line.startswith("//")
            and "// indirect" not in line
        ):
            info.direct_dependencies += 1
    return info


def collect_metrics(repo: Path) -> CodeMetrics:
    metrics = CodeMetrics()
    for path in repo.rglob("*.go"):
        if _SKIP_DIRS.intersection(path.relative_to(repo).parts):
            continue
        metrics.go_files += 1
        if path.name.endswith("_test.go"):
            metrics.test_files += 1
        try:
            metrics.lines_of_code += sum(
                1 for ln in path.read_text(errors="ignore").splitlines() if ln.strip()
            )
        except OSError:
            continue
    return metrics


def _rel(repo: Path, file: str) -> str:
    try:
        return str(Path(file).resolve().relative_to(repo.resolve()))
    except ValueError:
        return file.removeprefix("./")


async def list_packages(repo: Path) -> list[str]:
    res = await run(["go", "list", "./..."], cwd=repo, env=go_cache_env(repo), timeout=300)
    return res.stdout.split() if res.ok else []


async def run_go_vet(repo: Path) -> ToolRun:
    res = await run(["go", "vet", "./..."], cwd=repo, env=go_cache_env(repo), timeout=600)
    findings = []
    for line in res.stderr.splitlines():
        if m := _POS_RE.match(line.strip()):
            findings.append(
                ToolFinding(
                    tool="go vet", file=_rel(repo, m["file"]), line=int(m["line"]), message=m["msg"]
                )
            )
    # vet exits non-zero both for findings and for build errors; only the latter is a failure.
    ok = res.ok or bool(findings)
    return ToolRun(
        tool="go vet",
        available=True,
        ok=ok,
        findings=findings[:MAX_FINDINGS_PER_TOOL],
        error=None if ok else res.output[-1500:],
    )


async def run_staticcheck(repo: Path) -> ToolRun:
    if shutil.which("staticcheck") is None:
        return ToolRun(tool="staticcheck", available=False, ok=False)
    res = await run(
        ["staticcheck", "-f", "json", "./..."], cwd=repo, env=go_cache_env(repo), timeout=900
    )
    findings = []
    for line in res.stdout.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        loc = item.get("location", {})
        findings.append(
            ToolFinding(
                tool="staticcheck",
                file=_rel(repo, loc.get("file", "")),
                line=loc.get("line"),
                message=f"{item.get('code')}: {item.get('message')}",
            )
        )
    ok = res.returncode in (0, 1)  # 1 == findings present
    return ToolRun(
        tool="staticcheck",
        available=True,
        ok=ok,
        findings=findings[:MAX_FINDINGS_PER_TOOL],
        error=None if ok else res.output[-1500:],
    )


async def run_govulncheck(repo: Path) -> ToolRun:
    if shutil.which("govulncheck") is None:
        return ToolRun(tool="govulncheck", available=False, ok=False)
    res = await run(["govulncheck", "./..."], cwd=repo, env=go_cache_env(repo), timeout=900)
    findings = [
        ToolFinding(tool="govulncheck", file="go.mod", message=f"reachable vulnerability {m['id']}")
        for line in res.stdout.splitlines()
        if (m := _VULN_RE.match(line.strip()))
    ]
    ok = res.returncode in (0, 3)  # 3 == vulnerabilities found
    return ToolRun(
        tool="govulncheck",
        available=True,
        ok=ok,
        findings=findings,
        error=None if ok else res.output[-1500:],
    )
