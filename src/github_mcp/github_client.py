"""Thin async GitHub REST client with retries, rate-limit handling, ETag caching and typed errors.

Every failure is raised as a GitHubError subclass whose message is written for the
model to read: it says what went wrong and what to do next. The MCP layer turns
these into `isError=True` tool results instead of crashing the server.
"""

import asyncio
import logging
import random
import re
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger("github_mcp.client")

API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
URL_PREFIX_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/", re.IGNORECASE)


class GitHubError(Exception):
    """Base class. `str(err)` is safe to show to the model and the user."""


class InvalidInputError(GitHubError):
    pass


class AuthError(GitHubError):
    pass


class NotFoundError(GitHubError):
    pass


class RateLimitError(GitHubError):
    def __init__(self, message: str, reset_at: float | None = None):
        super().__init__(message)
        self.reset_at = reset_at


class UpstreamError(GitHubError):
    pass


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 3          # total tries for 5xx / network errors
    base_delay: float = 0.5        # seconds, doubled each attempt, plus jitter
    max_rate_limit_wait: float = 10.0  # wait this long at most for a reset; otherwise fail fast


@dataclass
class Page:
    """One page of a list endpoint, and whether GitHub says another page follows."""
    items: list[Any]
    has_next: bool


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = API_URL,
        timeout: float = 15.0,
        retry: RetryPolicy = RetryPolicy(),
        transport: httpx.AsyncBaseTransport | None = None,
        etag_cache_size: int = 256,
    ):
        if not token:
            raise AuthError("GITHUB_TOKEN is not set. Create a read-only fine-grained PAT and export it.")
        self._retry = retry
        self._login: str | None = None
        # Conditional requests: a 304 Not Modified does not count against GitHub's rate limit.
        self._etags: OrderedDict[str, tuple[str, Any, bool]] = OrderedDict()
        self._etag_cache_size = etag_cache_size
        self.cache_hits = 0
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "github-mcp/1.1",
            },
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---- identity / input helpers -------------------------------------------------

    async def login(self) -> str:
        """The authenticated user's login, cached after the first call."""
        if self._login is None:
            me = await self.get("/user")
            self._login = me["login"]
        return self._login

    async def resolve_repo(self, repo: str, *, allow_bare_name: bool) -> tuple[str, str]:
        """Validate `owner/name`. With allow_bare_name, `name` resolves to the token owner's repo.

        Also accepts what people paste: `https://github.com/o/n`, `github.com/o/n`, `o/n.git`.
        """
        repo = URL_PREFIX_RE.sub("", (repo or "").strip()).rstrip("/")
        if repo.endswith(".git"):
            repo = repo[:-4]
        if REPO_RE.match(repo):
            owner, name = repo.split("/", 1)
            return owner, name
        if allow_bare_name and NAME_RE.match(repo):
            return await self.login(), repo
        raise InvalidInputError(
            f"Invalid repo {repo!r}. Use the full 'owner/name' form, e.g. 'octocat/hello-world'. "
            "Call list_repositories to find the exact name."
        )

    # ---- HTTP core ---------------------------------------------------------------------

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return (await self._request(path, params))[0]

    async def get_page(self, path: str, params: dict[str, Any] | None = None) -> Page:
        data, has_next = await self._request(path, params)
        return Page(data, has_next)

    async def collect(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        keep: Callable[[Any], bool] = lambda _: True,
        max_pages: int = 5,
        per_page: int = 100,
    ) -> tuple[list[Any], bool]:
        """Page through a list endpoint. Returns (kept items, complete).

        `complete` is False when max_pages was reached with more pages left, so any count
        derived from the items is a lower bound.
        """
        items: list[Any] = []
        for page_no in range(1, max_pages + 1):
            page = await self.get_page(path, {**(params or {}), "per_page": per_page, "page": page_no})
            items += [i for i in page.items if keep(i)]
            if not page.has_next:
                return items, True
        return items, False

    async def _request(self, path: str, params: dict[str, Any] | None) -> tuple[Any, bool]:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        key = f"{path}?{sorted(params.items())}"
        cached = self._etags.get(key)
        headers = {"If-None-Match": cached[0]} if cached else {}
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = await self._http.get(path, params=params, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt >= self._retry.max_attempts:
                    raise UpstreamError(f"Could not reach GitHub after {attempt} attempts ({type(exc).__name__}).") from exc
                await self._backoff(attempt, reason=type(exc).__name__)
                continue

            if resp.status_code == 304 and cached:
                self.cache_hits += 1
                self._etags.move_to_end(key)
                return cached[1], cached[2]

            if resp.status_code < 400:
                data, has_next = resp.json(), 'rel="next"' in resp.headers.get("link", "")
                if etag := resp.headers.get("etag"):
                    self._remember(key, (etag, data, has_next))
                return data, has_next

            wait = self._rate_limit_wait(resp)
            if wait is not None:
                if wait <= self._retry.max_rate_limit_wait and attempt < self._retry.max_attempts:
                    log.warning("rate limited on %s, sleeping %.1fs", path, wait)
                    await asyncio.sleep(wait)
                    continue
                reset_at = time.time() + wait
                raise RateLimitError(
                    "GitHub API rate limit reached. Retry after "
                    f"{time.strftime('%H:%M:%S UTC', time.gmtime(reset_at))}. Do not retry immediately.",
                    reset_at=reset_at,
                )

            if resp.status_code >= 500 and attempt < self._retry.max_attempts:
                await self._backoff(attempt, reason=f"HTTP {resp.status_code}")
                continue

            raise self._to_error(resp, path)

    def _remember(self, key: str, entry: tuple[str, Any, bool]) -> None:
        self._etags[key] = entry
        self._etags.move_to_end(key)
        while len(self._etags) > self._etag_cache_size:
            self._etags.popitem(last=False)

    async def _backoff(self, attempt: int, reason: str) -> None:
        delay = self._retry.base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
        log.warning("retrying after %s (attempt %d), sleeping %.2fs", reason, attempt, delay)
        await asyncio.sleep(delay)

    @staticmethod
    def _rate_limit_wait(resp: httpx.Response) -> float | None:
        """Seconds to wait if this response is a primary or secondary rate limit, else None."""
        if resp.status_code not in (403, 429):
            return None
        if "retry-after" in resp.headers:  # secondary rate limit
            return float(resp.headers["retry-after"])
        if resp.headers.get("x-ratelimit-remaining") == "0":  # primary rate limit
            reset = float(resp.headers.get("x-ratelimit-reset", time.time() + 60))
            return max(0.0, reset - time.time()) + 1
        if resp.status_code == 429:
            return 60.0
        return None

    @staticmethod
    def _to_error(resp: httpx.Response, path: str) -> GitHubError:
        try:
            detail = resp.json().get("message", "")
        except ValueError:
            detail = resp.text[:200]
        if resp.status_code == 401:
            return AuthError("GitHub rejected the token (401). It is missing, expired or revoked.")
        if resp.status_code == 403:
            return AuthError(f"Token lacks permission for {path} (403): {detail}")
        if resp.status_code == 404:
            return NotFoundError(
                f"Not found: {path}. The repo may not exist, may be misspelled, or the token cannot see it. "
                "Call list_repositories to check the exact name."
            )
        if resp.status_code == 409:
            return UpstreamError(f"GitHub returned 409 for {path}: {detail} (an empty repository has no commits).")
        if resp.status_code == 422:
            return InvalidInputError(f"GitHub rejected the request parameters (422): {detail}")
        return UpstreamError(f"GitHub returned HTTP {resp.status_code}: {detail}")
