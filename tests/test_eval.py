"""Offline tests for the evaluation harness, the benchmark data and the seed script."""

import argparse
import json
import re
from pathlib import Path

import httpx
import optimize
import pytest
import run_eval
from backends import AgentResult
from fake_github import FAKE_LOGIN, FakeGitHub
from grading import check_args, example_args
from seed_data import APP, LIB, REPOS, build_world
from seed_repos import Seeder

EVAL = Path(__file__).resolve().parent.parent / "eval"
SUBS = {"repo": "me/app", "repo2": "me/lib", "repo_name": "app", "repo2_name": "lib", "owner": "me"}


def jsonl(name):
    return [json.loads(line) for line in (EVAL / name).read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------- question files

@pytest.mark.parametrize("name", ["questions.jsonl", "heldout.jsonl"])
def test_question_files_are_well_formed(name):
    from github_mcp.descriptions import TOOL_NAMES

    qs = jsonl(name)
    assert len({q["id"] for q in qs}) == len(qs)
    for q in qs:
        assert q["expected_tool"] in TOOL_NAMES
        q["question"].format(**SUBS)  # every placeholder is known
        assert "expected_args" in q


def test_main_set_is_balanced_and_heldout_is_disjoint():
    main, held = jsonl("questions.jsonl"), jsonl("heldout.jsonl")
    counts = {}
    for q in main:
        counts[q["expected_tool"]] = counts.get(q["expected_tool"], 0) + 1
    assert set(counts.values()) == {10}
    assert not {q["question"] for q in main} & {q["question"] for q in held}


# ---------------------------------------------------------------- argument grading

DEFAULTS = {"state": "all", "limit": 20, "label": None}


@pytest.mark.parametrize("expected,actual,ok", [
    ({"repo": "{repo}"}, {"repo": "me/app"}, True),
    ({"repo": "{repo}"}, {"repo": "app"}, True),
    ({"repo": "{repo}"}, {"repo": "https://github.com/Me/App"}, True),
    ({"repo": "{repo}"}, {"repo": "me/other"}, False),
    ({"repo": None}, {}, True),
    ({"repo": None}, {"repo": "me/app"}, False),
    ({"query": {"contains": "auth"}}, {"query": "Authentication"}, True),
    ({"query": {"contains": "auth"}}, {}, False),
    ({"since_days": {"range": [6, 8]}}, {"since_days": 7}, True),
    ({"since_days": {"range": [6, 8]}}, {"since_days": "7"}, True),
    ({"since_days": {"range": [6, 8]}}, {}, False),
    ({"state": "all"}, {}, True),  # equals the default, so omitting it is fine
    ({"state": "closed"}, {}, False),
    ({"label": {"any": ["bug", None]}}, {}, True),
    ({"label": {"any": ["bug", None]}}, {"label": "BUG"}, True),
    ({"label": {"any": ["bug", None]}}, {"label": "docs"}, False),
    ({"limit": 5}, {"limit": 5}, True),
    ({"author": "{owner}"}, {"author": "ME"}, True),
])
def test_check_args(expected, actual, ok):
    assert (check_args(expected, actual, DEFAULTS, SUBS) == []) is ok


def test_example_args_satisfy_their_own_spec():
    for q in jsonl("questions.jsonl") + jsonl("heldout.jsonl"):
        args = example_args(q["expected_args"], SUBS, bare_repo=q["category"] == "bare_name")
        assert check_args(q["expected_args"], args, DEFAULTS, SUBS) == [], q["id"]


# ---------------------------------------------------------------- benchmark world

def test_agent_answers_match_the_seeded_world():
    """The regexes in agent_questions.jsonl must agree with the data seed_data.py creates."""
    world = build_world("me")
    app, lib = world[f"me/{APP}"], world[f"me/{LIB}"]
    open_app = [i for i in app["issues"] if i["state"] == "open"]
    facts = {
        "a01": str(len(open_app)),
        "a02": str(sum("bug" in [lb["name"] for lb in i["labels"]] for i in open_app)),
        "a04": max(open_app, key=lambda i: i["comments"])["title"],
        "a06": lib["repo"]["license"]["spdx_id"],
        "a07": app["commits"][0]["commit"]["message"],
        "a08": str(sum(p["state"] == "open" for p in app["pulls"])),
        "a09": next(p["title"] for p in app["pulls"] if p["state"] == "closed" and not p["merged_at"]),
        "a10": next(c["commit"]["author"]["name"] for c in app["commits"] if c["author"] is None),
        "a13": f"{lib['repo']['language']} {sum(i['state'] == 'open' for i in lib['issues'])}",
    }
    expect = {q["id"]: q["expect"] for q in jsonl("agent_questions.jsonl")}
    for qid, fact in facts.items():
        for pattern in expect[qid]:
            assert re.search(pattern, fact, re.IGNORECASE), (qid, pattern, fact)
    # The trap the agent set is built on: the repo's open_issues_count mixes in PRs.
    assert app["repo"]["open_issues_count"] == 8 and len(open_app) == 6


def test_fake_github_search_and_pull_flags():
    gh = FakeGitHub()
    status, body = gh.route("/search/issues", {"q": f"memory leak user:{FAKE_LOGIN} is:issue state:closed"})
    assert status == 200 and [i["title"] for i in body["items"]] == ["Memory leak in the background sync worker"]
    status, body = gh.route(f"/repos/{FAKE_LOGIN}/{APP}/pulls", {"state": "closed"})
    assert sorted(bool(p["merged_at"]) for p in body) == [False, True]
    assert gh.route(f"/repos/{FAKE_LOGIN}/nope", {})[0] == 404


# ---------------------------------------------------------------- end to end through the real server

def fake_env(monkeypatch):
    for key in ("GITHUB_API_URL", "GITHUB_TOKEN", "EVAL_REPO", "EVAL_REPO2"):
        monkeypatch.setenv(key, "")  # registers the old value so it is restored after the test
    return run_eval.use_fake_github()


def eval_args(**overrides):
    base = dict(mode="select", toolset="v2", backend="oracle", model=None, effort="low", ollama_url="",
                questions=None, limit=None, runs=1, max_steps=8, markdown=False)
    return argparse.Namespace(**{**base, **overrides})


async def test_oracle_scores_100_on_v2_and_v1_fails_only_bare_names(monkeypatch, tmp_path):
    monkeypatch.setattr(run_eval, "RESULTS_DIR", tmp_path)
    httpd = fake_env(monkeypatch)
    try:
        v2 = await run_eval.run(eval_args(toolset="v2"))
        v1 = await run_eval.run(eval_args(toolset="v1"))
    finally:
        httpd.shutdown()
    assert {r.outcome for r in v2} == {run_eval.CORRECT}
    failed = {r.id for r in v1 if r.outcome != run_eval.CORRECT}
    assert failed == {r.id for r in v1 if r.category == "bare_name"} and len(failed) == 10
    assert len(list(tmp_path.glob("select-v2-oracle-*.jsonl"))) == 1


class ScriptedAgent:
    """Stands in for an LLM in agent mode: looks up open issues, then answers with the total."""

    def set_tools(self, tools):
        self.names = {t.name for t in tools}

    async def run_agent(self, q, call_tool, max_steps=8):
        repo = re.search(r"[\w-]+/mcp-bench-[a-z]+", q["question"]).group(0)
        text, is_error = await call_tool("list_open_issues", {"repo": repo})
        total = json.loads(text)["total_open"]
        return AgentResult(f"There are {total} open issues.",
                           calls=[{"tool": "list_open_issues", "args": {"repo": repo}, "error": is_error}])


async def test_agent_mode_grades_answers(monkeypatch, tmp_path):
    monkeypatch.setattr(run_eval, "RESULTS_DIR", tmp_path)
    monkeypatch.setattr(run_eval, "make_backend", lambda args, subs: ScriptedAgent())
    httpd = fake_env(monkeypatch)
    try:
        rows = await run_eval.run(eval_args(mode="agent", backend="ollama", limit=2))
    finally:
        httpd.shutdown()
    assert [r.outcome for r in rows] == [run_eval.ANSWER_OK, run_eval.ANSWER_WRONG]  # a01 asks total (6), a02 bugs (3)
    assert rows[0].tool_calls[0]["tool"] == "list_open_issues"


# ---------------------------------------------------------------- reporting

def row(qid, outcome, run=0, tool="get_repository"):
    return run_eval.Row(qid, f"question {qid}", "direct", "get_repository", tool, {}, outcome, "", 0.1, run=run)


def test_report_uses_medians_across_runs_and_counts_confusions():
    rows = [row("q1", run_eval.CORRECT, 0), row("q2", run_eval.WRONG, 0, "list_repositories"),
            row("q1", run_eval.CORRECT, 1), row("q2", run_eval.CORRECT, 1),
            row("q1", run_eval.CORRECT, 2), row("q2", run_eval.WRONG, 2, "list_repositories")]
    text = run_eval.report(rows)
    assert "3 run(s) × 2 questions" in text and "50–100%" in text
    assert "2x  get_repository -> list_repositories" in text
    assert run_eval.correct_pct(rows) == 50
    md = run_eval.summary_table(rows, markdown=True)
    assert md.splitlines()[0].startswith("| Status | Count")


def test_compare_counts_fixed_and_regressed(tmp_path, capsys):
    before, after = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    before.write_text("\n".join(json.dumps(r.__dict__) for r in [row("q1", "✅ Correct Tool"), row("q2", run_eval.WRONG)]))
    after.write_text("\n".join(json.dumps(r.__dict__) for r in [row("q1", run_eval.FAILED), row("q2", run_eval.CORRECT)]))
    run_eval.compare(before, after)
    out = capsys.readouterr().out
    assert "Fixed: 1   Regressed: 1" in out and "REGRESSED q1" in out  # legacy label still reads as correct


# ---------------------------------------------------------------- optimizer

def test_optimizer_applies_only_valid_edits():
    from github_mcp.descriptions import V1

    edit = optimize.parse_edit('Sure! {"tools": {"get_repository": "Better.", "made_up": "x"}, '
                               '"params": {"repo": "' + "y" * 2000 + '"}}')
    new = optimize.apply_edit(V1, edit)
    assert new["tools"]["get_repository"] == "Better." and "made_up" not in new["tools"]
    assert new["params"]["repo"] == V1["params"]["repo"]  # too long, ignored
    assert V1["tools"]["get_repository"] == "Gets info about a repo."  # original untouched


def test_optimizer_prompt_never_contains_heldout_questions():
    rows = [run_eval.Row(q["id"], q["question"].format(**SUBS), q["category"], q["expected_tool"], None, {},
                         run_eval.WRONG, "", 0.0) for q in jsonl("questions.jsonl")]
    text = optimize.failures_text(rows, limit=100)
    assert all(q["question"].format(**SUBS) not in text for q in jsonl("heldout.jsonl"))


# ---------------------------------------------------------------- seed script

def test_seeder_makes_the_expected_writes():
    log = []

    def handler(request):
        log.append((request.method, request.url.path, json.loads(request.content or b"{}")))
        path = request.url.path
        if request.method == "GET" and path == "/user":
            return httpx.Response(200, json={"login": "me", "id": 7})
        if request.method == "GET" and "/git/ref/" in path:
            return httpx.Response(200, json={"object": {"sha": "abc"}})
        if request.method == "GET":
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(201, json={})

    seeder = Seeder(httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler)), pause=0)
    me = seeder.whoami()
    for rs in REPOS:
        seeder.seed_repo(rs, me)

    writes = [(m, p, b) for m, p, b in log if m != "GET"]
    created = [b["name"] for m, p, b in writes if p == "/user/repos"]
    assert created == [APP, LIB] and all(b["private"] for m, p, b in writes if p == "/user/repos")
    issues = [b for m, p, b in writes if m == "POST" and p.endswith("/issues")]
    assert len(issues) == sum(len(r.issues) for r in REPOS)
    merges = [p for m, p, b in writes if p.endswith("/merge")]
    assert merges == [f"/repos/me/{APP}/pulls/11/merge"]  # 10 issues, then the first PR is #11
    first_commit = next(b for m, p, b in writes if "/contents/" in p)
    assert first_commit["author"]["email"] == "7+me@users.noreply.github.com"
    license_commit = next(b for m, p, b in writes if p.endswith("/contents/LICENSE"))
    import base64
    assert "Copyright (c) 2026 me" in base64.b64decode(license_commit["content"]).decode()
