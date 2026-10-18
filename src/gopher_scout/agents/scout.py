"""Scout agent: finds candidate Go CLI repositories via GitHub search.

Deterministic on purpose: discovery is a filtering problem, not a reasoning problem,
so it costs no tokens and gives repeatable results.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from gopher_scout.agents.base import Agent
from gopher_scout.config import DiscoveryConfig
from gopher_scout.db import repository as repo
from gopher_scout.db.session import session_scope
from gopher_scout.services.github import GitHubClient


@dataclass(frozen=True)
class ScoutResult:
    seen: int
    new: int


def build_query(topic: str, cfg: DiscoveryConfig, now: datetime) -> str:
    since = (now - timedelta(days=cfg.pushed_within_days)).date().isoformat()
    return (
        f"language:Go topic:{topic} stars:{cfg.min_stars}..{cfg.max_stars} "
        f"pushed:>={since} archived:false fork:false"
    )


def selection_score(item: dict[str, Any], cfg: DiscoveryConfig, now: datetime) -> float:
    """0-10. Favours mid-sized, recently active repos with a backlog and a licence.

    Mid-sized matters: very large projects review slowly and are heavily guarded; tiny
    ones often have nobody to merge.
    """
    stars = max(int(item.get("stargazers_count", 0)), 1)
    sweet_spot = math.log10(3000)
    stars_score = max(0.0, 1 - abs(math.log10(stars) - sweet_spot) / 1.5)

    pushed = _parse_dt(item.get("pushed_at")) or now
    age_days = max((now - pushed).days, 0)
    recency = max(0.0, 1 - age_days / max(cfg.pushed_within_days, 1))

    backlog = min(int(item.get("open_issues_count", 0)), 100) / 100
    licensed = 1.0 if item.get("license") else 0.0

    return round(10 * (0.35 * stars_score + 0.35 * recency + 0.2 * backlog + 0.1 * licensed), 2)


def _parse_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class ScoutAgent(Agent):
    name = "scout"

    async def run(self, gh: GitHubClient) -> ScoutResult:
        cfg = self.settings.discovery
        now = datetime.now(UTC)
        excluded = {e.lower() for e in cfg.exclude}
        found: dict[str, dict[str, Any]] = {}

        for topic in cfg.topics:
            query = build_query(topic, cfg, now)
            self.log.info("searching: %s", query)
            for item in await gh.search_repositories(query, cfg.per_topic_limit):
                name = item["full_name"]
                if name.lower() not in excluded:
                    found.setdefault(name, item)

        new = 0
        with session_scope() as s:
            for name, item in found.items():
                project, created = repo.upsert_project(
                    s,
                    name,
                    html_url=item["html_url"],
                    description=item.get("description"),
                    stars=item.get("stargazers_count", 0),
                    forks=item.get("forks_count", 0),
                    open_issues=item.get("open_issues_count", 0),
                    topics=item.get("topics", []),
                    license=(item.get("license") or {}).get("spdx_id"),
                    default_branch=item.get("default_branch", "main"),
                    pushed_at=_parse_dt(item.get("pushed_at")),
                    selection_score=selection_score(item, cfg, now),
                )
                if created:
                    new += 1
                    repo.log_event(s, "project", project.id, self.name, "discovered", name)
        self.log.info("%d repos matched, %d new", len(found), new)
        return ScoutResult(seen=len(found), new=new)
