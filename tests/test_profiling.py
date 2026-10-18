from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gopher_scout.agents.profiler import detect_ai_policy, readme_intro
from gopher_scout.agents.scout import build_query, selection_score
from gopher_scout.config import DiscoveryConfig
from gopher_scout.services.go_tools import collect_metrics, parse_go_mod

GO_MOD = """module github.com/acme/tool

go 1.22

require github.com/spf13/cobra v1.8.0

require (
\tgithub.com/charmbracelet/lipgloss v0.10.0
\tgolang.org/x/term v0.20.0
\tgithub.com/inconshreveable/mousetrap v1.1.0 // indirect
)
"""


def test_parse_go_mod() -> None:
    info = parse_go_mod(GO_MOD)
    assert info.module_path == "github.com/acme/tool"
    assert info.go_version == "1.22"
    assert info.direct_dependencies == 3


def test_collect_metrics_skips_vendor(tmp_path: Path) -> None:
    (tmp_path / "main.go").write_text("package main\n\nfunc main() {}\n")
    (tmp_path / "main_test.go").write_text("package main\n")
    (tmp_path / "vendor" / "x").mkdir(parents=True)
    (tmp_path / "vendor" / "x" / "x.go").write_text("package x\n")
    m = collect_metrics(tmp_path)
    assert (m.go_files, m.test_files, m.lines_of_code) == (2, 1, 3)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("We do not accept AI-generated pull requests.", "restrictive"),
        ("Contributions written by LLMs will be closed without review.", "restrictive"),
        ("If you used AI tools, please disclose it in the PR description.", "permissive"),
        ("Please run make test before sending a PR.", "none_found"),
        ("The AIR tool is great.", "none_found"),
    ],
)
def test_detect_ai_policy(text: str, expected: str) -> None:
    policy, _ = detect_ai_policy({"CONTRIBUTING.md": text})
    assert policy == expected


def test_scout_query_and_score() -> None:
    cfg = DiscoveryConfig(topics=["cli"], min_stars=500, max_stars=20000, pushed_within_days=60)
    now = datetime(2026, 10, 18, tzinfo=UTC)
    assert build_query("cli", cfg, now) == (
        "language:Go topic:cli stars:500..20000 pushed:>=2026-08-19 archived:false fork:false"
    )
    fresh_mid = {
        "stargazers_count": 3000,
        "pushed_at": now.isoformat(),
        "open_issues_count": 80,
        "license": {"spdx_id": "MIT"},
    }
    stale_huge = {
        "stargazers_count": 19000,
        "pushed_at": (now - timedelta(days=59)).isoformat(),
        "open_issues_count": 0,
        "license": None,
    }
    assert selection_score(fresh_mid, cfg, now) > 8.5
    assert selection_score(stale_huge, cfg, now) < selection_score(fresh_mid, cfg, now) / 2


def test_readme_intro_skips_badges_html_and_code() -> None:
    readme = """# Tool

<p align="center"><img src="logo.png"></p>

[![Build](https://ci/badge.svg)](https://ci) [![Go](https://go/badge.svg)](https://go)

`tool` syncs your [dotfiles](https://example.com) across machines without a server.

```sh
tool sync
```

It works offline and resolves conflicts interactively, with a dry-run mode for safety.

## Installation

brew install tool
"""
    assert readme_intro(readme) == (
        "`tool` syncs your dotfiles across machines without a server.\n\n"
        "It works offline and resolves conflicts interactively, with a dry-run mode for safety."
    )
    assert readme_intro("# Only a title\n") is None
    # A paragraph that just repeats the GitHub description is dropped.
    assert readme_intro(
        readme, skip="`tool` syncs your dotfiles across machines without a server 🚀"
    ) == ("It works offline and resolves conflicts interactively, with a dry-run mode for safety.")
