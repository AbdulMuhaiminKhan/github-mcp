#!/usr/bin/env python3
"""A tiny offline GitHub REST API serving the benchmark world from seed_data.py.

It implements only what the MCP server calls: /user, /user/repos, /repos/{o}/{n},
/repos/{o}/{n}/issues, /repos/{o}/{n}/pulls, /repos/{o}/{n}/commits and /search/issues,
with pagination (Link headers), ETags (304s) and GitHub-style 404s.

Uses:
  python eval/run_eval.py --fake-github ...       # starts it automatically
  python eval/fake_github.py --port 8765          # standalone, then GITHUB_API_URL=http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from seed_data import build_world

FAKE_LOGIN = "bench-user"


def _matches_words(item: dict[str, Any], words: list[str]) -> bool:
    text = f"{item['title']} {item.get('body') or ''}".lower()
    return all(w.lower().strip('"\'') in text for w in words)


class FakeGitHub:
    def __init__(self, login: str = FAKE_LOGIN):
        self.login = login
        self.world = build_world(login)

    # Each handler returns (status, json body) for one GET.
    def route(self, path: str, q: dict[str, str]) -> tuple[int, Any]:
        parts = [p for p in path.split("/") if p]
        if parts == ["user"]:
            return 200, {"login": self.login, "id": 1}
        if parts == ["user", "repos"]:
            repos = [w["repo"] for w in self.world.values()]
            if q.get("visibility") == "public":
                repos = [r for r in repos if not r["private"]]
            elif q.get("visibility") == "private":
                repos = [r for r in repos if r["private"]]
            key = {"full_name": "full_name", "created": "created_at"}.get(q.get("sort", ""), "pushed_at")
            return 200, sorted(repos, key=lambda r: r[key], reverse=key != "full_name")
        if parts == ["search", "issues"]:
            return 200, self.search(q.get("q", ""))
        if len(parts) >= 3 and parts[0] == "repos":
            w = self.world.get(f"{parts[1]}/{parts[2]}")
            if w is None:
                return 404, {"message": "Not Found"}
            rest = parts[3:]
            if not rest:
                return 200, w["repo"]
            if rest == ["issues"]:
                return 200, self.issues(w, q)
            if rest == ["pulls"]:
                state = q.get("state", "open")
                return 200, [p for p in reversed(w["pulls"]) if state == "all" or p["state"] == state]
            if rest == ["commits"]:
                return self.commits(w, q)
        return 404, {"message": "Not Found"}

    def issues(self, w: dict[str, Any], q: dict[str, str]) -> list[dict[str, Any]]:
        state = q.get("state", "open")
        wanted = [lb for lb in q.get("labels", "").split(",") if lb]
        items = [dict(i) for i in w["issues"]] + [{**p, "pull_request": {"url": p["html_url"]}} for p in w["pulls"]]
        items = [i for i in items if state == "all" or i["state"] == state]
        items = [i for i in items if all(lb in {x["name"] for x in i["labels"]} for lb in wanted)]
        return sorted(items, key=lambda i: i["created_at"], reverse=q.get("direction", "desc") == "desc")

    def commits(self, w: dict[str, Any], q: dict[str, str]) -> tuple[int, Any]:
        if q.get("sha") not in (None, "", "main"):
            return 404, {"message": f"No commit found for SHA: {q['sha']}"}
        items = w["commits"]
        if since := q.get("since"):
            cutoff = datetime.fromisoformat(since.replace("Z", "+00:00"))
            items = [c for c in items
                     if datetime.fromisoformat(c["commit"]["author"]["date"].replace("Z", "+00:00")) >= cutoff]
        if author := q.get("author"):
            a = author.lower()
            items = [c for c in items if a in ((c["author"] or {}).get("login", "").lower(),
                                               c["commit"]["author"]["email"].lower())]
        return 200, items

    def search(self, query: str) -> dict[str, Any]:
        words, scope_repos, state, kind = [], list(self.world), None, None
        for tok in query.split():
            key, _, val = tok.partition(":")
            if val and key == "repo":
                scope_repos = [val] if val in self.world else []
            elif val and key == "user":
                scope_repos = [r for r in self.world if r.split("/")[0] == val]
            elif val and key in ("state", "is") and val in ("open", "closed"):
                state = val
            elif val and key == "is" and val in ("issue", "pr"):
                kind = val
            else:
                words.append(tok)
        items: list[dict[str, Any]] = []
        for full in scope_repos:
            w = self.world[full]
            if kind != "pr":
                items += w["issues"]
            if kind != "issue":
                items += [{**p, "pull_request": {"url": p["html_url"]}} for p in w["pulls"]]
        items = [i for i in items if (state is None or i["state"] == state) and _matches_words(i, words)]
        return {"total_count": len(items), "incomplete_results": False, "items": items}


def make_handler(gh: FakeGitHub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # keep stderr quiet
            pass

        def do_GET(self) -> None:
            if not self.headers.get("Authorization", "").startswith("Bearer "):
                return self._send(401, {"message": "Requires authentication"})
            url = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            status, body = gh.route(url.path, q)
            link = ""
            if status == 200 and isinstance(body, list):  # paginate list endpoints like GitHub
                per_page, page = min(int(q.get("per_page", 30)), 100), int(q.get("page", 1))
                start = (page - 1) * per_page
                if start + per_page < len(body):
                    link = f'<{url.path}?{urlencode({**q, "page": page + 1})}>; rel="next"'
                body = body[start:start + per_page]
            elif status == 200 and "items" in body:
                body = {**body, "items": body["items"][: min(int(q.get("per_page", 30)), 100)]}
            self._send(status, body, link)

        def _send(self, status: int, body: Any, link: str = "") -> None:
            raw = json.dumps(body).encode()
            etag = '"' + hashlib.sha1(raw).hexdigest() + '"'
            if status == 200 and self.headers.get("If-None-Match") == etag:
                self.send_response(304)
                self.send_header("ETag", etag)
                self.end_headers()
                return
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            if status == 200:
                self.send_header("ETag", etag)
            if link:
                self.send_header("Link", link)
            self.end_headers()
            self.wfile.write(raw)

    return Handler


def start(port: int = 0, login: str = FAKE_LOGIN) -> tuple[ThreadingHTTPServer, str]:
    """Start the fake API on a background thread. Returns (server, base_url)."""
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(FakeGitHub(login)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--login", default=FAKE_LOGIN)
    args = ap.parse_args()
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(FakeGitHub(args.login)))
    print(f"Fake GitHub on http://127.0.0.1:{args.port} as {args.login!r} "
          f"(repos: {', '.join(FakeGitHub(args.login).world)}). Ctrl+C to stop.")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
