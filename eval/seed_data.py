"""The benchmark world: two small private repos with known issues, pull requests and commits.

One spec feeds three things, so they can never disagree:
  - eval/seed_repos.py creates it on real GitHub (private repos in your account),
  - eval/fake_github.py serves it offline (CI and tokenless runs),
  - eval/agent_questions.jsonl has answers that tests/test_eval.py checks against it.

Issue and PR numbers are assigned in this order: a repo's issues first, then its PRs.
Dates are days before "now" (the seeding time, or the fake server's start time).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

APP, LIB = "mcp-bench-app", "mcp-bench-lib"
OTHER_AUTHOR = {"name": "Sam Lee", "email": "sam.lee@example.com"}  # a non-GitHub committer

MIT = """MIT License

Copyright (c) 2026 {owner}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

ISC = """ISC License

Copyright (c) 2026 {owner}

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
"""


@dataclass
class CommitSpec:
    path: str
    content: str
    message: str
    days_ago: float
    by_other: bool = False  # True: committed as OTHER_AUTHOR instead of the token owner


@dataclass
class IssueSpec:
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    closed: bool = False
    comments: int = 0


@dataclass
class PullSpec:
    title: str
    body: str
    branch: str
    path: str
    content: str
    outcome: str  # "open", "draft", "merged" (squash) or "closed" (rejected)


@dataclass
class RepoSpec:
    name: str
    description: str
    language: str
    license_spdx: str
    topics: list[str]
    commits: list[CommitSpec]
    issues: list[IssueSpec]
    pulls: list[PullSpec]
    release_commit: CommitSpec | None = None  # pushed after the PRs are merged, so it is the newest


REPOS: list[RepoSpec] = [
    RepoSpec(
        name=APP,
        description="Demo Flask task tracker used to benchmark a GitHub MCP server",
        language="Python",
        license_spdx="MIT",
        topics=["mcp-benchmark", "flask", "demo"],
        commits=[
            CommitSpec("README.md", "# mcp-bench-app\n\nA tiny Flask task tracker.\n", "Initial commit", 60),
            CommitSpec("LICENSE", MIT, "Add MIT license", 59),
            CommitSpec("app/main.py", "from flask import Flask\n\napp = Flask(__name__)\nTASKS = []\n",
                       "Add task model and CRUD routes", 45),
            CommitSpec("app/auth.py", "def login(user, password):\n    return user == 'demo'\n",
                       "Add session-based authentication", 30, by_other=True),
            CommitSpec("app/export.py", "import csv\n\ndef export(tasks, fh):\n    csv.writer(fh).writerows(tasks)\n",
                       "Add CSV export endpoint", 20),
            CommitSpec("app/sync.py", "CACHE = []\n\ndef sync(item):\n    CACHE.append(item)\n",
                       "Add background sync worker", 12, by_other=True),
            CommitSpec("app/main.py", "from flask import Flask\n\napp = Flask(__name__)\nTASKS = []\n\n"
                       "def valid(title):\n    return bool(title.strip())\n",
                       "Validate task titles before saving", 5),
            CommitSpec("requirements.txt", "flask==3.0.3\nrequests==2.31.0\n", "Pin Flask and requests versions", 2),
        ],
        issues=[
            IssueSpec("Login fails with an expired session token",
                      "Authentication breaks when the session token expires: the login page loops.", ["bug"], comments=1),
            IssueSpec("Crash on startup when the config file is missing",
                      "The app raises FileNotFoundError at startup if config.toml does not exist.", ["bug"]),
            IssueSpec("Request timeout when exporting a large CSV",
                      "Exporting more than 10k tasks hits the 30 second request timeout.", ["bug"], comments=3),
            IssueSpec("Add a dark mode toggle", "A dark theme for the task list, remembered per user.",
                      ["enhancement"], comments=2),
            IssueSpec("Support Docker deployment", "Ship a Dockerfile and a compose file.", ["enhancement"]),
            IssueSpec("README install steps are outdated", "The README still says Python 3.8.", ["documentation"]),
            IssueSpec("Memory leak in the background sync worker",
                      "Memory grows without bound because the sync cache is never cleared.", ["bug"],
                      closed=True, comments=2),
            IssueSpec("Add rate limiting to the public API", "Limit clients to 60 requests per minute.",
                      ["enhancement"], closed=True),
            IssueSpec("Typo on the login page", "'Pasword' should be 'Password'.", ["good first issue"], closed=True),
            IssueSpec("Upgrade Flask to 3.x", "Flask 2 is out of support.", [], closed=True, comments=1),
        ],
        pulls=[
            PullSpec("Fix memory leak in the background sync worker", "Clears the cache after each sync.",
                     "fix-sync-leak", "app/sync.py",
                     "CACHE = []\n\ndef sync(item):\n    CACHE.append(item)\n    CACHE.clear()\n", "merged"),
            PullSpec("Add dark mode stylesheet", "Work in progress for the dark mode issue.", "dark-mode",
                     "static/dark.css", "body { background: #111; color: #eee; }\n", "draft"),
            PullSpec("Bump requests to 2.32", "Security update.", "bump-requests", "requirements.txt",
                     "flask==3.0.3\nrequests==2.32.3\n", "open"),
            PullSpec("Experiment: switch to SQLite storage", "Trying SQLite instead of the in-memory list.",
                     "sqlite-experiment", "app/storage.py", "import sqlite3\n", "closed"),
        ],
        release_commit=CommitSpec("CHANGELOG.md", "# Changelog\n\n## 0.3.0\n- Fix memory leak in sync worker\n",
                                  "Release v0.3.0", 0),
    ),
    RepoSpec(
        name=LIB,
        description="Small TypeScript HTTP client library used to benchmark a GitHub MCP server",
        language="TypeScript",
        license_spdx="ISC",
        topics=["mcp-benchmark", "typescript", "http-client"],
        commits=[
            CommitSpec("README.md", "# mcp-bench-lib\n\nA tiny typed HTTP client.\n", "Initial commit", 50),
            CommitSpec("LICENSE", ISC, "Add ISC license", 49.5),
            CommitSpec("src/client.ts", "export async function get(url: string): Promise<unknown> {\n"
                       "  return (await fetch(url)).json();\n}\n", "Add HTTP client with typed responses", 40),
            CommitSpec("src/parse.ts", "export function parse(text: string): unknown {\n  return JSON.parse(text);\n}\n",
                       "Add parse() helper", 25),
            CommitSpec("src/auth.ts", "export const bearer = (t: string) => ({ Authorization: `Bearer ${t}` });\n",
                       "Add token authentication helpers", 15, by_other=True),
            CommitSpec("package.json", '{\n  "name": "mcp-bench-lib",\n  "version": "0.1.0"\n}\n',
                       "Add package.json", 6),
        ],
        issues=[
            IssueSpec("Add a retry option to the HTTP client", "Retry 5xx responses with backoff.",
                      ["enhancement"], comments=1),
            IssueSpec("Type definitions missing for parse()", "parse() returns unknown; callers want generics.", ["bug"]),
            IssueSpec("Document the authentication helpers", "The README does not mention bearer().", ["documentation"]),
            IssueSpec("Docker image fails to build on ARM", "npm ci fails on arm64 runners.", ["bug"],
                      closed=True, comments=1),
            IssueSpec("Timeout option is ignored", "get() never passes the timeout to fetch.", ["bug"], closed=True),
        ],
        pulls=[
            PullSpec("Add retry option to HTTP client", "Closes the retry issue.", "add-retry", "src/retry.ts",
                     "export const retries = 3;\n", "open"),
        ],
    ),
]


# ---------------------------------------------------------------------------- derived world

def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def build_world(owner: str, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """The state GitHub ends up in after seeding, in GitHub's own JSON shapes (trimmed).

    Used by the fake GitHub server and by the tests that check agent-question answers.
    """
    now = now or datetime.now(timezone.utc)
    owner_author = {"name": owner, "email": f"{owner}@users.noreply.github.com"}
    world: dict[str, dict[str, Any]] = {}
    for rs in REPOS:
        full = f"{owner}/{rs.name}"
        commits: list[dict[str, Any]] = []

        def add_commit(message: str, when: datetime, other: bool, n: int, _commits=commits, _full=full) -> None:
            who = OTHER_AUTHOR if other else owner_author
            _commits.append({
                "sha": hashlib.sha1(f"{_full}:{n}:{message}".encode()).hexdigest(),
                "commit": {"message": message, "author": {**who, "date": _iso(when)}},
                "author": None if other else {"login": owner},
            })

        for n, c in enumerate(rs.commits):
            add_commit(c.message, now - timedelta(days=c.days_ago), c.by_other, n)

        issues: list[dict[str, Any]] = []
        number = 0
        created_base = now - timedelta(days=max(c.days_ago for c in rs.commits) - 1)
        for n, i in enumerate(rs.issues):
            number += 1
            created = created_base + timedelta(hours=6 * n)
            issues.append({
                "number": number, "title": i.title, "body": i.body,
                "state": "closed" if i.closed else "open",
                "labels": [{"name": lb} for lb in i.labels],
                "user": {"login": owner}, "comments": i.comments,
                "created_at": _iso(created), "closed_at": _iso(now - timedelta(days=1)) if i.closed else None,
                "html_url": f"https://github.com/{full}/issues/{number}",
                "repository_url": f"https://api.github.com/repos/{full}",
            })
        pulls: list[dict[str, Any]] = []
        for n, pr in enumerate(rs.pulls):
            number += 1
            created = now - timedelta(days=3, hours=-n)
            merged = pr.outcome == "merged"
            pulls.append({
                "number": number, "title": pr.title, "body": pr.body,
                "state": "open" if pr.outcome in ("open", "draft") else "closed",
                "draft": pr.outcome == "draft", "merged_at": _iso(now - timedelta(hours=2)) if merged else None,
                "user": {"login": owner}, "head": {"ref": pr.branch}, "base": {"ref": "main"},
                "created_at": _iso(created), "html_url": f"https://github.com/{full}/pull/{number}",
                "labels": [], "comments": 0, "repository_url": f"https://api.github.com/repos/{full}",
            })
            if merged:
                add_commit(f"{pr.title} (#{number})", now - timedelta(hours=2), False, 100 + n)
        if rs.release_commit:
            add_commit(rs.release_commit.message, now - timedelta(minutes=30), False, 999)

        commits.sort(key=lambda c: c["commit"]["author"]["date"], reverse=True)
        open_issues = sum(1 for i in issues if i["state"] == "open")
        open_prs = sum(1 for p in pulls if p["state"] == "open")
        world[full] = {
            "repo": {
                "full_name": full, "name": rs.name, "owner": {"login": owner},
                "description": rs.description, "language": rs.language, "private": True,
                "stargazers_count": 0, "forks_count": 0, "subscribers_count": 1, "watchers_count": 0,
                "default_branch": "main", "license": {"spdx_id": rs.license_spdx}, "topics": rs.topics,
                "created_at": commits[-1]["commit"]["author"]["date"],
                "pushed_at": commits[0]["commit"]["author"]["date"],
                "updated_at": commits[0]["commit"]["author"]["date"],
                "open_issues_count": open_issues + open_prs,
            },
            "issues": issues,
            "pulls": pulls,
            "commits": commits,
        }
    return world
