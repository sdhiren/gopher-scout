"""Thin GitHub REST client (search, repo metadata, file contents, issues).

Uses the public API rather than scraping HTML: it is faster, structured, and within
GitHub's terms of service.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from typing import Any, Self

import httpx

from gopher_scout.domain.schemas import IssueSummary

log = logging.getLogger(__name__)

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(
        self, token: str | None, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "gopher-scout",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.AsyncClient(
            base_url=API, headers=headers, timeout=30.0, transport=transport
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._http.aclose()

    async def _get(self, path: str, **params: Any) -> Any:
        resp = await self._http.get(path, params=params or None)
        if resp.status_code == 404:
            return None
        if resp.status_code == 403 and resp.headers.get("x-ratelimit-remaining") == "0":
            reset = datetime.fromtimestamp(int(resp.headers.get("x-ratelimit-reset", "0")))
            raise GitHubError(f"GitHub rate limit exhausted; resets at {reset:%H:%M:%S}")
        if resp.is_error:
            raise GitHubError(f"GET {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    async def search_repositories(self, query: str, limit: int) -> list[dict[str, Any]]:
        data = await self._get(
            "/search/repositories", q=query, sort="stars", order="desc", per_page=min(limit, 100)
        )
        return list((data or {}).get("items", []))[:limit]

    async def get_repo(self, full_name: str) -> dict[str, Any] | None:
        result: dict[str, Any] | None = await self._get(f"/repos/{full_name}")
        return result

    async def get_file(self, full_name: str, path: str) -> str | None:
        data = await self._get(f"/repos/{full_name}/contents/{path}")
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            return None
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")

    async def count_merged_prs(self, full_name: str, since: datetime) -> int | None:
        data = await self._get(
            "/search/issues",
            q=f"repo:{full_name} is:pr is:merged merged:>={since:%Y-%m-%d}",
            per_page=1,
        )
        return int(data["total_count"]) if data else None

    async def list_open_issues(self, full_name: str, limit: int = 50) -> list[IssueSummary]:
        data = await self._get(
            f"/repos/{full_name}/issues", state="open", sort="updated", per_page=min(limit, 100)
        )
        return [_issue(i) for i in data or []]

    async def search_related(
        self, full_name: str, keywords: str, limit: int = 10
    ) -> list[IssueSummary]:
        """Issues and PRs (open or closed) that mention the given keywords."""
        data = await self._get(
            "/search/issues", q=f"repo:{full_name} {keywords}", per_page=min(limit, 100)
        )
        return [_issue(i) for i in (data or {}).get("items", [])]


def _issue(raw: dict[str, Any]) -> IssueSummary:
    return IssueSummary(
        number=raw["number"],
        title=raw["title"],
        url=raw["html_url"],
        labels=[lbl["name"] for lbl in raw.get("labels", [])],
        is_pr="pull_request" in raw,
        state=raw.get("state", "open"),
    )
