#!/usr/bin/env python3
"""Evaluation harness for the GitHub MCP server.

Mode `select` (default), the tool-selection benchmark. For each question it:
  1. starts the real MCP server over stdio (with the chosen toolset) and reads its tool list,
  2. sends the question plus those tools to an LLM and records which tool it picked,
  3. if the pick matches the expected tool, executes that call through MCP,
  4. buckets the outcome: Correct / Wrong Arguments / Wrong Tool / Tool Failed.

Mode `agent`, the end-to-end benchmark: the model may call tools for several turns and then
answers in words; the answer is checked against facts from the seeded benchmark repos.

Backends:
  ollama     a local model via Ollama's tool calling. $0. (default)
  anthropic  Claude via the Anthropic API. Costs a little per run; see GUIDE.md.
  oracle     always picks the expected tool and arguments: a self-test of harness + server + data.

Data:
  real GitHub     EVAL_REPO / EVAL_REPO2 in .env (ideally the repos eval/seed_repos.py creates)
  --fake-github   an offline GitHub serving the same seeded repos. No token needed.

Usage:
  python eval/run_eval.py --toolset v1 --backend ollama --model qwen2.5:7b --runs 3
  python eval/run_eval.py --toolset v2 --questions eval/heldout.jsonl
  python eval/run_eval.py --mode agent --toolset v2 --backend ollama
  python eval/run_eval.py --fake-github --backend oracle --toolset v2 --min-correct 100
  python eval/run_eval.py --compare eval/results/A.jsonl eval/results/B.jsonl
  python eval/run_eval.py --report eval/results/A.jsonl --markdown
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from mcp import Client
from mcp.client.stdio import StdioServerParameters

sys.path.insert(0, str(Path(__file__).resolve().parent))
from backends import AnthropicBackend, OllamaBackend, OracleBackend  # noqa: E402
from grading import check_args, fill, schema_defaults  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = ROOT / "eval" / "results"
DEFAULT_QUESTIONS = {"select": ROOT / "eval" / "questions.jsonl", "agent": ROOT / "eval" / "agent_questions.jsonl"}
DEFAULT_MODEL = {"anthropic": "claude-opus-5-5", "ollama": "qwen2.5:7b", "oracle": "oracle"}

CORRECT, WRONG_ARGS, WRONG, FAILED = "✅ Correct", "🟡 Wrong Arguments", "⚠️ Wrong Tool", "❌ Tool Failed"
SELECT_OUTCOMES = (CORRECT, WRONG_ARGS, WRONG, FAILED)
ANSWER_OK, ANSWER_WRONG, NO_ANSWER = "✅ Correct Answer", "❌ Wrong Answer", "⚠️ No Answer"
AGENT_OUTCOMES = (ANSWER_OK, ANSWER_WRONG, NO_ANSWER)
LEGACY = {"✅ Correct Tool": CORRECT}  # results files written before argument grading existed


@dataclass
class Row:
    id: str
    question: str
    category: str
    expected_tool: str
    actual_tool: str | None
    args: dict[str, Any]
    outcome: str
    detail: str
    latency_s: float
    run: int = 0
    mode: str = "select"
    toolset: str = ""
    backend: str = ""
    model: str = ""
    split: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    answer: str | None = None


# ---------------------------------------------------------------------------- helpers

def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def substitutions() -> dict[str, str]:
    repo, repo2 = os.environ.get("EVAL_REPO", ""), os.environ.get("EVAL_REPO2", "")
    if "/" not in repo or "/" not in repo2:
        sys.exit("Set EVAL_REPO and EVAL_REPO2 to real 'owner/name' repos (see .env.example), "
                 "or pass --fake-github to use the offline benchmark repos.")
    return {"repo": repo, "repo2": repo2, "repo_name": repo.split("/", 1)[1],
            "repo2_name": repo2.split("/", 1)[1], "owner": repo.split("/", 1)[0]}


def load_questions(path: Path, subs: dict[str, str]) -> list[dict[str, Any]]:
    questions = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            q = json.loads(line)
            q["question"] = q["question"].format(**subs)
            questions.append(q)
    return questions


def toolset_label(toolset: str) -> str:
    return Path(toolset).stem if toolset.endswith(".json") else toolset


def make_backend(args: argparse.Namespace, subs: dict[str, str]) -> Any:
    model = args.model or DEFAULT_MODEL[args.backend]
    if args.backend == "anthropic":
        return AnthropicBackend(model, args.effort)
    if args.backend == "oracle":
        return OracleBackend(subs)
    return OllamaBackend(model, args.ollama_url)


def server_params(toolset: str) -> StdioServerParameters:
    toolset = str(Path(toolset).resolve()) if toolset.endswith(".json") else toolset
    return StdioServerParameters(
        command=sys.executable,
        args=["-m", "github_mcp.server"],
        env={**os.environ, "GITHUB_MCP_TOOLSET": toolset, "PYTHONPATH": str(ROOT / "src"), "LOG_LEVEL": "WARNING"},
    )


def use_fake_github() -> Any:
    """Start the offline GitHub and point everything at it. Returns the server (keep it alive)."""
    from fake_github import FAKE_LOGIN, start
    from seed_data import REPOS

    httpd, url = start()
    os.environ.update({
        "GITHUB_API_URL": url, "GITHUB_TOKEN": "fake-token",
        "EVAL_REPO": f"{FAKE_LOGIN}/{REPOS[0].name}", "EVAL_REPO2": f"{FAKE_LOGIN}/{REPOS[1].name}",
    })
    return httpd


def _result_text(result: Any) -> str:
    return " ".join(getattr(c, "text", "") for c in (result.content or [])).strip()


# ---------------------------------------------------------------------------- run

async def run_select(mcp: Client, backend: Any, questions: list[dict[str, Any]], subs: dict[str, str],
                     meta: dict[str, Any], run: int) -> list[Row]:
    tools = (await mcp.list_tools()).tools
    backend.set_tools(tools)
    defaults = {t.name: schema_defaults(t.input_schema) for t in tools}
    rows = []
    for n, q in enumerate(questions, 1):
        start = time.perf_counter()
        choice = await backend.choose(q)
        if choice.tool != q["expected_tool"]:
            outcome, detail = WRONG, choice.note
        else:
            result = await mcp.call_tool(choice.tool, choice.args)
            if result.is_error:
                outcome, detail = FAILED, _result_text(result)[:300]
            else:
                problems = check_args(q.get("expected_args"), choice.args, defaults.get(choice.tool, {}), subs)
                outcome, detail = (WRONG_ARGS, "; ".join(problems)) if problems else (CORRECT, "")
        row = Row(q["id"], q["question"], q.get("category", ""), q["expected_tool"], choice.tool, choice.args,
                  outcome, detail, round(time.perf_counter() - start, 2), run=run,
                  input_tokens=choice.input_tokens, output_tokens=choice.output_tokens, **meta)
        rows.append(row)
        print(f"[run {run + 1} {n:02d}/{len(questions)}] {outcome:<20} expected={row.expected_tool:<20} "
              f"actual={row.actual_tool}", flush=True)
    return rows


async def run_agent(mcp: Client, backend: Any, questions: list[dict[str, Any]], subs: dict[str, str],
                    meta: dict[str, Any], run: int, max_steps: int) -> list[Row]:
    backend.set_tools((await mcp.list_tools()).tools)

    async def call_tool(name: str, args: dict[str, Any]) -> tuple[str, bool]:
        try:
            result = await mcp.call_tool(name, args)
        except Exception as exc:  # unknown tool name or malformed call: tell the model, keep going
            return f"Error: {exc}", True
        return _result_text(result)[:8000], bool(result.is_error)

    rows = []
    for n, q in enumerate(questions, 1):
        start = time.perf_counter()
        res = await backend.run_agent(q, call_tool, max_steps=max_steps)
        expect = fill(q["expect"], subs)
        if res.answer is None:
            outcome, detail = NO_ANSWER, res.note
        else:
            missing = [p for p in expect if not re.search(p, res.answer, re.IGNORECASE)]
            outcome = ANSWER_WRONG if missing else ANSWER_OK
            detail = f"missing {missing}" if missing else ""
        tools_used = [c["tool"] for c in res.calls]
        row = Row(q["id"], q["question"], "agent", ",".join(dict.fromkeys(tools_used)), None, {}, outcome, detail,
                  round(time.perf_counter() - start, 2), run=run, input_tokens=res.input_tokens,
                  output_tokens=res.output_tokens, tool_calls=res.calls, answer=res.answer, **meta)
        rows.append(row)
        print(f"[run {run + 1} {n:02d}/{len(questions)}] {outcome:<18} calls={len(res.calls)} "
              f"answer={(res.answer or '')[:90]!r}", flush=True)
    return rows


async def run(args: argparse.Namespace) -> list[Row]:
    subs = substitutions()
    if args.mode == "agent" and (subs["repo_name"], subs["repo2_name"]) != ("mcp-bench-app", "mcp-bench-lib"):
        sys.exit("Agent mode checks answers against the seeded benchmark repos. Point EVAL_REPO / EVAL_REPO2 at "
                 "mcp-bench-app / mcp-bench-lib (create them with eval/seed_repos.py), or use --fake-github.")
    if not os.environ.get("GITHUB_TOKEN"):
        sys.exit("GITHUB_TOKEN is not set.")
    qpath = Path(args.questions) if args.questions else DEFAULT_QUESTIONS[args.mode]
    questions = load_questions(qpath, subs)[: args.limit]
    backend = make_backend(args, subs)
    meta = {"mode": args.mode, "toolset": toolset_label(args.toolset), "backend": args.backend,
            "model": args.model or DEFAULT_MODEL[args.backend], "split": qpath.stem}

    rows: list[Row] = []
    async with Client(server_params(args.toolset)) as mcp:
        for r in range(args.runs):
            if args.mode == "agent":
                rows += await run_agent(mcp, backend, questions, subs, meta, r, args.max_steps)
            else:
                rows += await run_select(mcp, backend, questions, subs, meta, r)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    model = re.sub(r"[^A-Za-z0-9._-]", "-", meta["model"])
    out = RESULTS_DIR / f"{args.mode}-{meta['toolset']}-{args.backend}-{model}-{meta['split']}-{stamp}.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(asdict(row), ensure_ascii=False) + "\n")
    print(f"\nRun: mode={args.mode} toolset={meta['toolset']} backend={args.backend} model={meta['model']} "
          f"questions={qpath.name} ({len(questions)}) runs={args.runs}")
    print(report(rows, markdown=args.markdown))
    print(f"Saved {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}")
    return rows


# ---------------------------------------------------------------------------- reporting

def read_rows(path: Path) -> list[Row]:
    known = {f.name for f in fields(Row)}
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            data = {k: v for k, v in json.loads(line).items() if k in known}
            data["outcome"] = LEGACY.get(data["outcome"], data["outcome"])
            rows.append(Row(**data))
    return rows


def outcomes_for(rows: list[Row]) -> tuple[str, ...]:
    return AGENT_OUTCOMES if rows and rows[0].mode == "agent" else SELECT_OUTCOMES


def by_run(rows: list[Row]) -> dict[int, list[Row]]:
    runs: dict[int, list[Row]] = defaultdict(list)
    for r in rows:
        runs[r.run].append(r)
    return dict(sorted(runs.items()))


def correct_pct(rows: list[Row]) -> float:
    """Median (across runs) percentage of fully correct outcomes."""
    good = outcomes_for(rows)[0]
    return statistics.median(100 * sum(r.outcome == good for r in rr) / len(rr) for rr in by_run(rows).values())


def summary_table(rows: list[Row], markdown: bool = False) -> str:
    runs = by_run(rows)
    per_run = len(next(iter(runs.values()))) if runs else 0
    multi = len(runs) > 1
    head = ["Status", "Count", "Percentage"] + (["Range across runs"] if multi else [])
    lines = []
    for status in outcomes_for(rows):
        counts = [sum(r.outcome == status for r in rr) for rr in runs.values()] or [0]
        med = statistics.median(counts)
        pct = [round(100 * c / (per_run or 1)) for c in counts]
        cells = [status, f"{med:g}", f"{round(100 * med / (per_run or 1))}%"]
        if multi:
            cells.append(f"{min(pct)}–{max(pct)}%")
        lines.append(cells)
    if markdown:
        out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
        out += ["| " + " | ".join(c) + " |" for c in lines]
        return "\n".join(out)
    return "\n".join("\t".join(c) for c in [head] + lines)


def report(rows: list[Row], markdown: bool = False) -> str:
    if not rows:
        return "(no rows)"
    runs = by_run(rows)
    parts = ["", f"{len(runs)} run(s) × {len(next(iter(runs.values())))} questions"
             + (" (counts are medians across runs)" if len(runs) > 1 else ""), summary_table(rows, markdown), ""]
    if rows[0].mode == "select":
        tool_ok = statistics.median(100 * sum(r.outcome != WRONG for r in rr) / len(rr) for rr in runs.values())
        parts.append(f"Right tool chosen: {tool_ok:.0f}%   Fully correct (tool and arguments): {correct_pct(rows):.0f}%")
    tokens = [r.input_tokens for r in rows if r.input_tokens]
    if tokens:
        outs = [r.output_tokens or 0 for r in rows if r.input_tokens]
        parts.append(f"Tokens per question: {statistics.mean(tokens):.0f} in, {statistics.mean(outs):.0f} out "
                     f"(mean)   Latency: {statistics.mean(r.latency_s for r in rows):.2f}s per question")
    parts.append("")
    if rows[0].mode == "agent":
        wrong = [r for r in rows if r.outcome != ANSWER_OK]
        if wrong:
            parts.append("Wrong or missing answers:")
            parts += [f"  {r.id} [{r.expected_tool or 'no tools'}] {r.detail}: {(r.answer or '')[:120]!r}" for r in wrong]
            parts.append("")
        return "\n".join(parts)
    confusions = Counter((r.expected_tool, r.actual_tool or "(no tool)") for r in rows if r.outcome == WRONG)
    if confusions:
        parts.append("Wrong-tool confusions (expected -> actual):")
        parts += [f"  {n}x  {exp} -> {act}" for (exp, act), n in confusions.most_common()]
        parts.append("")
    for title, status in (("Wrong arguments:", WRONG_ARGS), ("Tool failures:", FAILED)):
        bad = [r for r in rows if r.outcome == status]
        if bad:
            parts.append(title)
            parts += [f"  {r.id} {r.expected_tool}({json.dumps(r.args)}): {r.detail[:140]}" for r in bad]
            parts.append("")
    by_cat: dict[str, Counter] = {}
    for r in rows:
        by_cat.setdefault(r.category.split(":")[0] or "-", Counter())[r.outcome] += 1
    parts.append("Correct by question category" + (" (all runs)" if len(runs) > 1 else "") + ":")
    for cat, c in sorted(by_cat.items()):
        parts.append(f"  {cat:<10} {c[CORRECT]}/{sum(c.values())}")
    return "\n".join(parts) + "\n"


def majority(rows: list[Row]) -> dict[str, str]:
    """Each question's most common outcome across runs."""
    votes: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        votes[r.id][r.outcome] += 1
    return {qid: c.most_common(1)[0][0] for qid, c in votes.items()}


def compare(base_path: Path, new_path: Path, markdown: bool = False) -> None:
    base, new = read_rows(base_path), read_rows(new_path)
    good = outcomes_for(base)[0]
    print(f"BEFORE ({base_path.name})\n{summary_table(base, markdown)}\n")
    print(f"AFTER ({new_path.name})\n{summary_table(new, markdown)}\n")
    b, n = majority(base), majority(new)
    text = {r.id: r.question for r in base}
    fixed = [q for q in b if b[q] != good and n.get(q) == good]
    regressed = [q for q in b if b[q] == good and q in n and n[q] != good]
    print(f"Fixed: {len(fixed)}   Regressed: {len(regressed)}   (majority outcome per question)")
    for q in regressed:
        print(f"  REGRESSED {q}: {text[q]}  -> {n[q]}")


def main() -> None:
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["select", "agent"], default="select")
    ap.add_argument("--toolset", default="v1", help="v1, v1b, v2, or a path to a toolset .json file")
    ap.add_argument("--backend", choices=["anthropic", "ollama", "oracle"], default="ollama")
    ap.add_argument("--model", help="default: claude-opus-5-5 (anthropic) or qwen2.5:7b (ollama)")
    ap.add_argument("--effort", default="low", choices=["low", "medium", "high"], help="Claude effort level")
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--questions", help="questions file (default: questions.jsonl, or agent_questions.jsonl)")
    ap.add_argument("--limit", type=int, default=None, help="only run the first N questions")
    ap.add_argument("--runs", type=int, default=1, help="repeat the whole set N times; medians are reported")
    ap.add_argument("--max-steps", type=int, default=8, help="agent mode: tool-calling turns before giving up")
    ap.add_argument("--fake-github", action="store_true", help="use the offline benchmark repos (no token needed)")
    ap.add_argument("--min-correct", type=float, help="exit with status 1 if median correct %% is below this")
    ap.add_argument("--markdown", action="store_true", help="print summary tables as Markdown")
    ap.add_argument("--report", type=Path, help="re-print the summary for a saved results file")
    ap.add_argument("--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"))
    args = ap.parse_args()

    if args.report:
        print(report(read_rows(args.report), markdown=args.markdown))
        return
    if args.compare:
        compare(*args.compare, markdown=args.markdown)
        return
    _fake = use_fake_github() if args.fake_github else None  # noqa: F841  (keeps the server alive)
    rows = asyncio.run(run(args))
    if args.min_correct is not None and correct_pct(rows) < args.min_correct:
        sys.exit(f"FAIL: {correct_pct(rows):.0f}% correct is below --min-correct {args.min_correct:g}%")


if __name__ == "__main__":
    main()
