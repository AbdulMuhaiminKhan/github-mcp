#!/usr/bin/env python3
"""Create the two private benchmark repos (mcp-bench-app, mcp-bench-lib) in your GitHub account.

It writes the world defined in seed_data.py: backdated commits, labelled open and closed issues
with comments, and pull requests that are open, draft, merged and closed. After it finishes,
point EVAL_REPO / EVAL_REPO2 at the two repos and every benchmark question has a real answer.

This needs a token that can WRITE, which the MCP server's token must never have. Use a separate,
short-lived fine-grained token and delete it when done:
  Repository access: All repositories      (needed to create new repos)
  Permissions (Read and write): Administration, Contents, Issues, Pull requests
Put it in .env as GITHUB_SEED_TOKEN (never commit it).

  python eval/seed_repos.py --dry-run      # print the plan, write nothing
  python eval/seed_repos.py                # create both repos (about 70 API calls, ~2 minutes)

Safe to re-run: a repo that already exists is skipped, not modified. To start over, delete the
repo on GitHub (Settings > Danger zone) and run it again.
"""

from __future__ import annotations

import argparse
import base64
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from seed_data import OTHER_AUTHOR, REPOS, CommitSpec, RepoSpec

ROOT = Path(__file__).resolve().parent.parent
WRITE_PAUSE = 1.0  # seconds between writes; GitHub's secondary limit for content creation is ~80/min


class Seeder:
    def __init__(self, http: httpx.Client, *, pause: float = WRITE_PAUSE, dry_run: bool = False):
        self.http, self.pause, self.dry_run = http, pause, dry_run
        self.calls = 0
        self.now = datetime.now(timezone.utc)

    def request(self, method: str, path: str, body: dict[str, Any] | None = None, ok: tuple[int, ...] = ()) -> Any:
        self.calls += 1
        if self.dry_run:
            print(f"  {method:<6} {path}")
            return {}
        for attempt in range(5):
            resp = self.http.request(method, path, json=body)
            if resp.status_code in (403, 429) and ("retry-after" in resp.headers
                                                     or resp.headers.get("x-ratelimit-remaining") == "0"):
                wait = float(resp.headers.get("retry-after", 60))
                print(f"  rate limited, waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            if resp.status_code >= 500 and attempt < 4:
                time.sleep(2 ** attempt)
                continue
            break
        if resp.status_code >= 400 and resp.status_code not in ok:
            sys.exit(f"{method} {path} failed with {resp.status_code}: {resp.text[:300]}")
        if method != "GET" and self.pause:
            time.sleep(self.pause)
        return resp.json() if resp.content else {}

    # ------------------------------------------------------------------ steps

    def whoami(self) -> dict[str, Any]:
        if self.dry_run:
            return {"login": "<you>", "id": 0}
        return self.request("GET", "/user")

    def exists(self, owner: str, name: str) -> bool:
        if self.dry_run:
            return False
        return self.http.get(f"/repos/{owner}/{name}").status_code == 200

    def commit(self, full: str, spec: CommitSpec, me: dict[str, Any], branch: str = "main",
               when: datetime | None = None) -> None:
        who = OTHER_AUTHOR if spec.by_other else {
            "name": me["login"], "email": f"{me['id']}+{me['login']}@users.noreply.github.com"}
        date = (when or self.now - timedelta(days=spec.days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
        current = None if self.dry_run else self.http.get(f"/repos/{full}/contents/{spec.path}", params={"ref": branch})
        body: dict[str, Any] = {
            "message": spec.message,
            "content": base64.b64encode(spec.content.replace("{owner}", me["login"]).encode()).decode(),
            "branch": branch,
            "author": {**who, "date": date},
            "committer": {**who, "date": date},
        }
        if current is not None and current.status_code == 200:
            body["sha"] = current.json()["sha"]
        self.request("PUT", f"/repos/{full}/contents/{spec.path}", body)

    def seed_repo(self, rs: RepoSpec, me: dict[str, Any]) -> None:
        full = f"{me['login']}/{rs.name}"
        print(f"\n== {full}")
        if self.exists(me["login"], rs.name):
            print("   already exists, skipping (delete it on GitHub to re-seed)")
            return
        self.request("POST", "/user/repos", {
            "name": rs.name, "description": rs.description, "private": True,
            "has_issues": True, "auto_init": False,
        })
        print(f"   commits: {len(rs.commits)}")
        for c in rs.commits:  # the first PUT on an empty repo creates the main branch
            self.commit(full, c, me)
        self.request("PUT", f"/repos/{full}/topics", {"names": rs.topics})

        print(f"   issues: {len(rs.issues)}")
        for n, issue in enumerate(rs.issues, 1):
            self.request("POST", f"/repos/{full}/issues",
                         {"title": issue.title, "body": issue.body, "labels": issue.labels})
            for k in range(issue.comments):
                self.request("POST", f"/repos/{full}/issues/{n}/comments",
                             {"body": f"Benchmark comment {k + 1} on #{n}."})
            if issue.closed:
                self.request("PATCH", f"/repos/{full}/issues/{n}", {"state": "closed", "state_reason": "completed"})

        print(f"   pull requests: {len(rs.pulls)}")
        main_sha = "<sha>" if self.dry_run else self.request("GET", f"/repos/{full}/git/ref/heads/main")["object"]["sha"]
        number = len(rs.issues)
        for pr in rs.pulls:
            number += 1
            self.request("POST", f"/repos/{full}/git/refs", {"ref": f"refs/heads/{pr.branch}", "sha": main_sha})
            self.commit(full, CommitSpec(pr.path, pr.content, pr.title, 0), me, branch=pr.branch, when=self.now)
            self.request("POST", f"/repos/{full}/pulls", {
                "title": pr.title, "body": pr.body, "head": pr.branch, "base": "main",
                "draft": pr.outcome == "draft",
            })
            if pr.outcome == "merged":
                self.request("PUT", f"/repos/{full}/pulls/{number}/merge", {"merge_method": "squash"})
            elif pr.outcome == "closed":
                self.request("PATCH", f"/repos/{full}/pulls/{number}", {"state": "closed"})

        if rs.release_commit:
            self.commit(full, rs.release_commit, me, when=datetime.now(timezone.utc))
        print("   done")


def load_dotenv(path: Path) -> None:
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> None:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the API calls without making them")
    ap.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    args = ap.parse_args()

    token = os.environ.get("GITHUB_SEED_TOKEN", "")
    if not token and not args.dry_run:
        sys.exit("Set GITHUB_SEED_TOKEN (a short-lived token with write access; see the top of this file).")
    http = httpx.Client(base_url=args.api_url, timeout=30, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "github-mcp-seed/1.0",
    })
    seeder = Seeder(http, dry_run=args.dry_run)
    me = seeder.whoami()
    for rs in REPOS:
        seeder.seed_repo(rs, me)
    print(f"\n{seeder.calls} API calls.")
    if not args.dry_run:
        print("\nAdd these to .env, then wait a minute so GitHub's search index catches up:")
        print(f"  EVAL_REPO={me['login']}/{REPOS[0].name}\n  EVAL_REPO2={me['login']}/{REPOS[1].name}")
        print("Make sure your read-only GITHUB_TOKEN can see these two repos, then delete GITHUB_SEED_TOKEN.")


if __name__ == "__main__":
    main()
