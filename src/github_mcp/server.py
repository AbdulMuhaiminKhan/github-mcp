"""Read-only GitHub MCP server.

Run:  GITHUB_TOKEN=... GITHUB_MCP_TOOLSET=v2 github-mcp      (stdio transport)

stdout carries the MCP protocol, so all logging goes to stderr.
GITHUB_API_URL points the server at another API root (GitHub Enterprise, or the eval's fake GitHub).
"""

import logging
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import Field

from .descriptions import load_toolset
from .github_client import API_URL, GitHubClient, GitHubError

log = logging.getLogger("github_mcp")
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)


def _trim(text: str | None, n: int = 280) -> str | None:
    if text is None:
        return None
    text = text.strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def build_server(toolset: str = "v1", client: GitHubClient | None = None) -> MCPServer:
    cfg = load_toolset(toolset)
    d, p = cfg["tools"], cfg["params"]
    bare_ok = cfg["allow_bare_repo_name"]
    gh = client or GitHubClient(
        os.environ.get("GITHUB_TOKEN", ""), base_url=os.environ.get("GITHUB_API_URL") or API_URL
    )

    @asynccontextmanager
    async def lifespan(_server: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await gh.aclose()

    server = MCPServer(name="github-readonly", version="1.1.0", lifespan=lifespan)

    async def call(fn, *args, **kwargs) -> Any:
        """Run a GitHub call, mapping expected failures to a ToolError the model can read."""
        try:
            return await fn(*args, **kwargs)
        except GitHubError as exc:
            log.info("tool error: %s", exc)
            raise ToolError(str(exc)) from exc

    Repo = Annotated[str, Field(description=p["repo"])]
    Limit = Annotated[int, Field(ge=1, le=50, description=p["limit"])]

    # -- 1. list_repositories ------------------------------------------------------------
    @server.tool(name="list_repositories", description=d["list_repositories"], annotations=READ_ONLY)
    async def list_repositories(
        visibility: Annotated[Literal["all", "public", "private"], Field(description=p["visibility"])] = "all",
        sort: Annotated[Literal["updated", "pushed", "created", "full_name"], Field(description=p["sort"])] = "updated",
        limit: Limit = 30,
    ) -> dict:
        data, complete = await call(
            gh.collect, "/user/repos",
            {"visibility": visibility, "sort": sort, "affiliation": "owner"}, max_pages=10,
        )
        shown = data[:limit]
        return {
            "total": len(data),
            "total_is_exact": complete,
            "returned": len(shown),
            "repositories": [
                {
                    "full_name": r["full_name"],
                    "description": _trim(r.get("description"), 160),
                    "language": r.get("language"),
                    "private": r["private"],
                    "stars": r["stargazers_count"],
                    "pushed_at": r.get("pushed_at"),
                }
                for r in shown
            ],
        }

    # -- 2. get_repository -----------------------------------------------------------------
    @server.tool(name="get_repository", description=d["get_repository"], annotations=READ_ONLY)
    async def get_repository(repo: Repo) -> dict:
        owner, name = await call(gh.resolve_repo, repo, allow_bare_name=bare_ok)
        r = await call(gh.get, f"/repos/{owner}/{name}")
        return {
            "full_name": r["full_name"],
            "description": r.get("description"),
            "language": r.get("language"),
            "stars": r["stargazers_count"],
            "forks": r["forks_count"],
            "watchers": r["subscribers_count"] if "subscribers_count" in r else r.get("watchers_count"),
            "default_branch": r["default_branch"],
            "license": (r.get("license") or {}).get("spdx_id"),
            "topics": r.get("topics", []),
            "private": r["private"],
            "created_at": r["created_at"],
            "pushed_at": r.get("pushed_at"),
            "open_issues_and_prs": r["open_issues_count"],
        }

    # -- 3. list_open_issues ---------------------------------------------------------------
    @server.tool(name="list_open_issues", description=d["list_open_issues"], annotations=READ_ONLY)
    async def list_open_issues(
        repo: Repo,
        label: Annotated[str | None, Field(description=p["label"])] = None,
        limit: Limit = 20,
    ) -> dict:
        owner, name = await call(gh.resolve_repo, repo, allow_bare_name=bare_ok)
        # The issues endpoint also returns PRs, so page through and drop them. That also gives
        # an exact total, which the model would otherwise guess from a truncated list.
        issues, complete = await call(
            gh.collect, f"/repos/{owner}/{name}/issues",
            {"state": "open", "labels": label, "sort": "created", "direction": "desc"},
            keep=lambda i: "pull_request" not in i,
        )
        shown = issues[:limit]
        return {
            "repo": f"{owner}/{name}",
            "label": label,
            "total_open": len(issues),
            "total_is_exact": complete,
            "returned": len(shown),
            "issues": [
                {
                    "number": i["number"],
                    "title": i["title"],
                    "labels": [lb["name"] for lb in i.get("labels", [])],
                    "author": (i.get("user") or {}).get("login"),
                    "comments": i.get("comments", 0),
                    "created_at": i["created_at"],
                    "url": i["html_url"],
                }
                for i in shown
            ],
        }

    # -- 4. search_issues ------------------------------------------------------------------
    @server.tool(name="search_issues", description=d["search_issues"], annotations=READ_ONLY)
    async def search_issues(
        query: Annotated[str, Field(max_length=200, description=p["query"])] = "",
        repo: Annotated[str | None, Field(description=p["repo"])] = None,
        state: Annotated[Literal["open", "closed", "all"], Field(description=p["state"])] = "all",
        limit: Limit = 20,
    ) -> dict:
        if repo:
            owner, name = await call(gh.resolve_repo, repo, allow_bare_name=bare_ok)
            scope = f"repo:{owner}/{name}"
        else:
            scope = f"user:{await call(gh.login)}"
        q = f"{query.strip()} {scope} is:issue".lstrip() + ("" if state == "all" else f" state:{state}")
        data = await call(gh.get, "/search/issues", {"q": q, "per_page": limit})
        return {
            "query": q,
            "total_count": data["total_count"],
            "returned": len(data["items"]),
            "issues": [
                {
                    "repo": i["repository_url"].split("/repos/", 1)[-1],
                    "number": i["number"],
                    "title": i["title"],
                    "state": i["state"],
                    "labels": [lb["name"] for lb in i.get("labels", [])],
                    "comments": i.get("comments", 0),
                    "closed_at": i.get("closed_at"),
                    "url": i["html_url"],
                }
                for i in data["items"]
            ],
        }

    # -- 5. list_pull_requests -------------------------------------------------------------
    @server.tool(name="list_pull_requests", description=d["list_pull_requests"], annotations=READ_ONLY)
    async def list_pull_requests(
        repo: Repo,
        state: Annotated[Literal["open", "closed", "all"], Field(description=p["pr_state"])] = "open",
        limit: Limit = 20,
    ) -> dict:
        owner, name = await call(gh.resolve_repo, repo, allow_bare_name=bare_ok)
        prs, complete = await call(
            gh.collect, f"/repos/{owner}/{name}/pulls",
            {"state": state, "sort": "created", "direction": "desc"}, max_pages=3,
        )
        shown = prs[:limit]
        return {
            "repo": f"{owner}/{name}",
            "state": state,
            "total": len(prs),
            "total_is_exact": complete,
            "returned": len(shown),
            "pull_requests": [
                {
                    "number": pr["number"],
                    "title": pr["title"],
                    "state": pr["state"],
                    "draft": pr.get("draft", False),
                    "merged": bool(pr.get("merged_at")),
                    "author": (pr.get("user") or {}).get("login"),
                    "head": (pr.get("head") or {}).get("ref"),
                    "base": (pr.get("base") or {}).get("ref"),
                    "created_at": pr["created_at"],
                    "url": pr["html_url"],
                }
                for pr in shown
            ],
        }

    # -- 6. get_recent_commits -------------------------------------------------------------
    @server.tool(name="get_recent_commits", description=d["get_recent_commits"], annotations=READ_ONLY)
    async def get_recent_commits(
        repo: Repo,
        since_days: Annotated[int | None, Field(ge=1, le=365, description=p["since_days"])] = None,
        author: Annotated[str | None, Field(max_length=100, description=p["author"])] = None,
        branch: Annotated[str | None, Field(max_length=255, description=p["branch"])] = None,
        limit: Limit = 10,
    ) -> dict:
        owner, name = await call(gh.resolve_repo, repo, allow_bare_name=bare_ok)
        since = (
            (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat(timespec="seconds")
            if since_days else None
        )
        page = await call(
            gh.get_page, f"/repos/{owner}/{name}/commits",
            {"since": since, "author": author, "sha": branch, "per_page": limit},
        )
        return {
            "repo": f"{owner}/{name}",
            "returned": len(page.items),
            "has_more": page.has_next,
            "commits": [
                {
                    "sha": c["sha"][:7],
                    "author": (c.get("author") or {}).get("login") or c["commit"]["author"]["name"],
                    "date": c["commit"]["author"]["date"],
                    "message": _trim(c["commit"]["message"].split("\n", 1)[0], 160),
                }
                for c in page.items
            ],
        }

    return server


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    toolset = os.environ.get("GITHUB_MCP_TOOLSET", "v2")
    log.info("starting github-readonly MCP server with toolset %s", toolset)
    build_server(toolset).run()  # stdio by default


if __name__ == "__main__":
    main()
