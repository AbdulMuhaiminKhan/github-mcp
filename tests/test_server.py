"""Offline tests: a fake GitHub (httpx.MockTransport) behind the real MCP server, called in-process."""

import json

import httpx
import pytest
from mcp import Client

from github_mcp.github_client import GitHubClient, RetryPolicy
from github_mcp.server import build_server

REPO = {
    "full_name": "me/app", "description": "demo", "language": "Python", "stargazers_count": 3,
    "forks_count": 1, "subscribers_count": 2, "default_branch": "main", "license": {"spdx_id": "MIT"},
    "topics": ["mcp"], "private": False, "created_at": "2025-01-01T00:00:00Z",
    "pushed_at": "2026-10-01T00:00:00Z", "open_issues_count": 4,
}


def make_client(handler, **retry) -> GitHubClient:
    return GitHubClient(
        "test-token",
        transport=httpx.MockTransport(handler),
        retry=RetryPolicy(base_delay=0, **retry),
    )


def payload(result):
    return result.structured_content or json.loads(result.content[0].text)


def router(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/user":
        return httpx.Response(200, json={"login": "me"})
    if path == "/repos/me/app":
        return httpx.Response(200, json=REPO)
    if path == "/repos/me/app/issues":
        return httpx.Response(200, json=[
            {"number": 1, "title": "bug", "labels": [{"name": "bug"}], "user": {"login": "a"},
             "comments": 0, "created_at": "2026-01-01T00:00:00Z", "html_url": "u1"},
            {"number": 2, "title": "a PR", "pull_request": {}, "labels": [], "user": {"login": "b"},
             "created_at": "2026-01-01T00:00:00Z", "html_url": "u2"},
        ])
    return httpx.Response(404, json={"message": "Not Found"})


async def test_tool_listing_matches_toolset():
    for toolset in ("v1", "v1b", "v2"):
        async with Client(build_server(toolset, make_client(router))) as c:
            names = {t.name for t in (await c.list_tools()).tools}
        assert names == {"list_repositories", "get_repository", "list_open_issues",
                         "search_issues", "list_pull_requests", "get_recent_commits"}


async def test_open_issues_excludes_pull_requests():
    async with Client(build_server("v2", make_client(router))) as c:
        res = await c.call_tool("list_open_issues", {"repo": "me/app"})
    assert not res.is_error
    assert [i["number"] for i in payload(res)["issues"]] == [1]
    assert payload(res)["total_open"] == 1 and payload(res)["total_is_exact"]


async def test_bare_repo_name_rejected_in_v1_resolved_in_v2():
    async with Client(build_server("v1", make_client(router))) as c:
        assert (await c.call_tool("get_repository", {"repo": "app"})).is_error
    async with Client(build_server("v2", make_client(router))) as c:
        res = await c.call_tool("get_repository", {"repo": "app"})
    assert not res.is_error and payload(res)["full_name"] == "me/app"


async def test_404_is_a_readable_tool_error():
    async with Client(build_server("v2", make_client(router))) as c:
        res = await c.call_tool("get_repository", {"repo": "me/missing"})
    assert res.is_error and "list_repositories" in res.content[0].text


async def test_schema_validation_rejects_bad_limit():
    async with Client(build_server("v2", make_client(router))) as c:
        res = await c.call_tool("list_open_issues", {"repo": "me/app", "limit": 500})
    assert res.is_error


async def test_retries_5xx_then_succeeds():
    calls = {"n": 0}

    def flaky(request):
        calls["n"] += 1
        return httpx.Response(502) if calls["n"] < 3 else router(request)

    async with Client(build_server("v2", make_client(flaky))) as c:
        res = await c.call_tool("get_repository", {"repo": "me/app"})
    assert not res.is_error and calls["n"] == 3


async def test_rate_limit_fails_fast_with_reset_time():
    def limited(request):
        return httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"},
                              json={"message": "API rate limit exceeded"})

    async with Client(build_server("v2", make_client(limited))) as c:
        res = await c.call_tool("get_repository", {"repo": "me/app"})
    assert res.is_error and "rate limit" in res.content[0].text


async def test_bad_token_is_reported():
    async with Client(build_server("v2", make_client(lambda r: httpx.Response(401, json={})))) as c:
        res = await c.call_tool("list_repositories", {})
    assert res.is_error and "401" in res.content[0].text


async def test_search_without_keyword_scopes_to_user_and_state():
    seen = {}

    def handler(request):
        if request.url.path == "/search/issues":
            seen["q"] = request.url.params["q"]
            return httpx.Response(200, json={"total_count": 0, "items": []})
        return router(request)

    async with Client(build_server("v2", make_client(handler))) as c:
        res = await c.call_tool("search_issues", {"state": "closed"})
    assert not res.is_error
    assert seen["q"] == "user:me is:issue state:closed"


def paged(items, request, per_page_default=30):
    """Serve `items` like a GitHub list endpoint: per_page/page params and a Link header."""
    per_page = int(request.url.params.get("per_page", per_page_default))
    page = int(request.url.params.get("page", 1))
    chunk = items[(page - 1) * per_page: page * per_page]
    headers = {"link": f'<{request.url}>; rel="next"'} if page * per_page < len(items) else {}
    return httpx.Response(200, json=chunk, headers=headers)


def issue(n, pr=False, labels=()):
    item = {"number": n, "title": f"t{n}", "labels": [{"name": lb} for lb in labels], "user": {"login": "a"},
            "comments": n % 3, "created_at": "2026-01-01T00:00:00Z", "html_url": f"u{n}"}
    if pr:
        item["pull_request"] = {}
    return item


async def test_open_issue_total_is_exact_beyond_limit_and_pages():
    # 130 open issues plus 90 PRs, spread over three pages: the old code reported count=20.
    items = [issue(n, pr=n % 5 < 2) for n in range(1, 221)]

    def handler(request):
        if request.url.path == "/repos/me/app/issues":
            return paged(items, request)
        return router(request)

    async with Client(build_server("v2", make_client(handler))) as c:
        res = await c.call_tool("list_open_issues", {"repo": "me/app", "limit": 5})
    body = payload(res)
    assert body["total_open"] == 132 and body["total_is_exact"] and body["returned"] == 5


async def test_list_repositories_reports_total():
    repos = [{**REPO, "full_name": f"me/r{n}", "private": n % 2 == 0} for n in range(75)]

    def handler(request):
        if request.url.path == "/user/repos":
            return paged(repos, request)
        return router(request)

    async with Client(build_server("v2", make_client(handler))) as c:
        body = payload(await c.call_tool("list_repositories", {"limit": 3}))
    assert body["total"] == 75 and body["returned"] == 3 and body["total_is_exact"]


async def test_list_pull_requests_shapes_merged_and_draft():
    prs = [
        {"number": 7, "title": "fix", "state": "closed", "draft": False, "merged_at": "2026-01-02T00:00:00Z",
         "user": {"login": "me"}, "head": {"ref": "fix"}, "base": {"ref": "main"},
         "created_at": "2026-01-01T00:00:00Z", "html_url": "p7"},
        {"number": 8, "title": "wip", "state": "open", "draft": True, "merged_at": None,
         "user": {"login": "me"}, "head": {"ref": "wip"}, "base": {"ref": "main"},
         "created_at": "2026-01-03T00:00:00Z", "html_url": "p8"},
    ]
    seen = {}

    def handler(request):
        if request.url.path == "/repos/me/app/pulls":
            seen["state"] = request.url.params["state"]
            return paged(prs, request)
        return router(request)

    async with Client(build_server("v2", make_client(handler))) as c:
        body = payload(await c.call_tool("list_pull_requests", {"repo": "me/app", "state": "all"}))
    assert seen["state"] == "all" and body["total"] == 2
    assert [(p["number"], p["merged"], p["draft"]) for p in body["pull_requests"]] == [(7, True, False), (8, False, True)]


async def test_recent_commits_uses_login_or_name_and_since():
    seen = {}

    def handler(request):
        if request.url.path == "/repos/me/app/commits":
            seen.update(request.url.params)
            return httpx.Response(200, json=[
                {"sha": "a" * 40, "author": {"login": "me"},
                 "commit": {"message": "Fix bug\n\nlong body", "author": {"name": "Me", "date": "2026-10-01T00:00:00Z"}}},
                {"sha": "b" * 40, "author": None,
                 "commit": {"message": "Docs", "author": {"name": "Sam Lee", "date": "2026-09-30T00:00:00Z"}}},
            ], headers={"link": '<x>; rel="next"'})
        return router(request)

    async with Client(build_server("v2", make_client(handler))) as c:
        body = payload(await c.call_tool("get_recent_commits", {"repo": "me/app", "since_days": 7, "limit": 2}))
    assert [c["author"] for c in body["commits"]] == ["me", "Sam Lee"]
    assert body["commits"][0] == {"sha": "aaaaaaa", "author": "me", "date": "2026-10-01T00:00:00Z", "message": "Fix bug"}
    assert body["has_more"] and "since" in seen and seen["per_page"] == "2"


async def test_etag_304_is_served_from_cache():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if request.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304)
        return httpx.Response(200, json=REPO, headers={"etag": '"v1"'})

    gh = make_client(handler)
    first = await gh.get("/repos/me/app")
    second = await gh.get("/repos/me/app")
    assert first == second == REPO and gh.cache_hits == 1 and calls["n"] == 2
    await gh.aclose()


@pytest.mark.parametrize("raw", ["me/app", "https://github.com/me/app", "github.com/me/app/",
                                 "http://www.github.com/me/app.git", " me/app.git "])
async def test_resolve_repo_accepts_pasted_forms(raw):
    gh = make_client(router)
    assert await gh.resolve_repo(raw, allow_bare_name=False) == ("me", "app")
    await gh.aclose()


def test_load_toolset_from_json(tmp_path):
    from github_mcp.descriptions import V2, load_toolset

    path = tmp_path / "custom.json"
    path.write_text(json.dumps({**V2, "tools": {**V2["tools"], "get_repository": "Custom."}}))
    assert load_toolset(str(path))["tools"]["get_repository"] == "Custom."
    path.write_text(json.dumps({**V2, "tools": {"get_repository": "Only one."}}))
    with pytest.raises(ValueError, match="missing"):
        load_toolset(str(path))
    with pytest.raises(ValueError, match="Unknown toolset"):
        load_toolset("v9")


def test_v1b_is_v1_text_with_bare_names():
    from github_mcp.descriptions import V1, V1B

    assert V1B["tools"] == V1["tools"] and V1B["params"] == V1["params"]
    assert V1B["allow_bare_repo_name"] and not V1["allow_bare_repo_name"]
