"""Tool and parameter descriptions, versioned so the eval can compare them.

v1  is deliberately vague: it is the baseline the evaluation measures.
v1b is v1's text with v2's input handling (bare repo names accepted). It exists for the
    ablation: v1 -> v1b isolates the behaviour fix, v1b -> v2 isolates the description rewrite.
v2  is the optimized set: each tool says what it returns, when to use it, and when NOT
    to use it (naming the tool to use instead), and every parameter states its format.

Select with GITHUB_MCP_TOOLSET=v1|v1b|v2, or a path to a JSON file with the same shape
(that is what eval/optimize.py writes).
"""

import json
from pathlib import Path
from typing import Any

TOOL_NAMES = (
    "list_repositories",
    "get_repository",
    "list_open_issues",
    "search_issues",
    "list_pull_requests",
    "get_recent_commits",
)
PARAM_NAMES = (
    "repo", "query", "limit", "visibility", "sort", "label", "state", "pr_state",
    "since_days", "author", "branch",
)

V1: dict[str, Any] = {
    "allow_bare_repo_name": False,
    "tools": {
        "list_repositories": "Gets repositories.",
        "get_repository": "Gets info about a repo.",
        "list_open_issues": "Gets issues.",
        "search_issues": "Searches GitHub.",
        "list_pull_requests": "Gets pull requests.",
        "get_recent_commits": "Gets recent activity for a repo.",
    },
    "params": {
        "repo": "The repo.",
        "query": "Search text.",
        "limit": "How many.",
        "visibility": "Visibility.",
        "sort": "Sort order.",
        "label": "Label.",
        "state": "State.",
        "pr_state": "State.",
        "since_days": "Days.",
        "author": "Author.",
        "branch": "Branch.",
    },
}

V1B: dict[str, Any] = {**V1, "allow_bare_repo_name": True}

V2: dict[str, Any] = {
    "allow_bare_repo_name": True,
    "tools": {
        "list_repositories": (
            "List the authenticated user's own GitHub repositories (name, description, language, "
            "stars, visibility, last push), plus the total number that match. Use for questions about "
            "WHICH repos exist, counting repos, or finding a repo by topic or language. "
            "Do NOT use for details of one named repo (use get_repository) or for issues, pull "
            "requests or commits."
        ),
        "get_repository": (
            "Get metadata for ONE repository: description, primary language, stars, forks, watchers, "
            "default branch, license, topics, created/last-pushed dates. "
            "Do NOT use to count or list issues (its open_issues_and_prs mixes issues with pull "
            "requests; use list_open_issues), for pull requests (use list_pull_requests), or to see "
            "who changed what (use get_recent_commits)."
        ),
        "list_open_issues": (
            "List the currently OPEN issues of ONE repository, newest first, optionally filtered by a "
            "single label, with the exact total of open issues. Pull requests are excluded. Use for "
            "'what is open', 'how many open issues', or 'open bugs in X'. "
            "Do NOT use for closed issues, keyword search, or issues across all repos (use "
            "search_issues), or for pull requests (use list_pull_requests)."
        ),
        "search_issues": (
            "Full-text search over issues (not code, not repos, not pull requests) across ALL of the "
            "user's repositories, or one repo if given. Supports open or closed state. Use when the "
            "question has a keyword ('issues mentioning auth'), asks about CLOSED issues, or spans "
            "every repo. Do NOT use to list a repo's open issues with no keyword (use "
            "list_open_issues) or to find repositories (use list_repositories)."
        ),
        "list_pull_requests": (
            "List the pull requests (PRs) of ONE repository: number, title, author, draft flag, "
            "branches, and whether it was merged, plus the total that match. Filter by open, closed "
            "or all. Use for 'open PRs', 'what is waiting for review', or 'which PRs were merged'. "
            "Do NOT use for issues (use list_open_issues or search_issues) or for the commit history "
            "of a branch (use get_recent_commits)."
        ),
        "get_recent_commits": (
            "List the most recent commits of ONE repository: SHA, author, date and message, optionally "
            "filtered by days back, author login, or branch. Use for 'what changed', 'who committed', "
            "'last commit', or activity in the last N days. "
            "Do NOT use for repository metadata such as stars or language (use get_repository) or for "
            "pull requests (use list_pull_requests)."
        ),
    },
    "params": {
        "repo": (
            "Repository as 'owner/name' (e.g. 'octocat/hello-world'). A bare name like 'hello-world' "
            "is resolved against the authenticated user's account."
        ),
        "query": (
            "Optional keywords to match in issue titles and bodies, e.g. 'login timeout'. Omit to "
            "match every issue in scope (e.g. all closed issues)."
        ),
        "limit": (
            "Maximum number of items to return (1-50). Totals are reported separately, so a small "
            "limit is fine when only a count is needed."
        ),
        "visibility": "Which repos to include: 'all', 'public' or 'private'.",
        "sort": "Order repos by 'updated' (most recently changed first), 'pushed', 'created' or 'full_name'.",
        "label": "Only return issues that have this exact label name, e.g. 'bug'. Omit for all open issues.",
        "state": "'open', 'closed' or 'all'. Default 'all'.",
        "pr_state": "'open' (default), 'closed' (merged or rejected) or 'all'.",
        "since_days": "Only commits from the last N days (1-365). Omit for the latest commits regardless of age.",
        "author": "Only commits by this GitHub login or email.",
        "branch": "Branch or tag name. Omit for the default branch.",
    },
}

TOOLSETS: dict[str, dict[str, Any]] = {"v1": V1, "v1b": V1B, "v2": V2}


def validate_toolset(cfg: dict[str, Any], source: str = "toolset") -> dict[str, Any]:
    """Check that a toolset describes every tool and parameter, and nothing else."""
    tools, params = cfg.get("tools", {}), cfg.get("params", {})
    missing = [n for n in TOOL_NAMES if not tools.get(n)] + [p for p in PARAM_NAMES if not params.get(p)]
    extra = sorted(set(tools) - set(TOOL_NAMES)) + sorted(set(params) - set(PARAM_NAMES))
    if missing or extra or not isinstance(cfg.get("allow_bare_repo_name"), bool):
        raise ValueError(f"Invalid {source}: missing={missing} unexpected={extra}")
    return cfg


def load_toolset(name_or_path: str) -> dict[str, Any]:
    """A built-in toolset name (v1, v1b, v2) or a path to a JSON toolset file."""
    if name_or_path in TOOLSETS:
        return TOOLSETS[name_or_path]
    path = Path(name_or_path)
    if path.suffix == ".json" and path.is_file():
        return validate_toolset(json.loads(path.read_text(encoding="utf-8")), str(path))
    raise ValueError(
        f"Unknown toolset {name_or_path!r}; expected one of {sorted(TOOLSETS)} or a path to a .json file"
    )
