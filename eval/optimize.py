#!/usr/bin/env python3
"""Automatic tool-description optimizer, with a held-out guard against overfitting.

Loop:
  1. Score the current toolset on the TRAIN questions (questions.jsonl) and the HELD-OUT
     questions (heldout.jsonl).
  2. Show an LLM the current descriptions and the TRAIN failures only (never the held-out
     questions) and ask it to rewrite the descriptions that caused them.
  3. Score the candidate. Keep it only if train accuracy improves AND held-out accuracy does
     not drop; otherwise discard it and try again from the best version so far.

Every candidate is saved under eval/optimized/ as a toolset JSON the server can load directly:
  GITHUB_MCP_TOOLSET=eval/optimized/best.json github-mcp

Usage:
  python eval/optimize.py --backend ollama --model qwen2.5:7b --start v1 --iterations 4
  python eval/optimize.py --fake-github --backend ollama --start v1     # no GitHub token needed
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import run_eval  # noqa: E402

from github_mcp.descriptions import load_toolset, validate_toolset  # noqa: E402

OUT_DIR = run_eval.ROOT / "eval" / "optimized"
MAX_DESCRIPTION = 1024

PROMPT = """An LLM is given these tools and must pick the right one (with the right arguments) for a user's
question about their GitHub account. Tool and parameter descriptions are all it sees.

Current descriptions (JSON):
{current}

Questions it got wrong on the last run:
{failures}

Rewrite the descriptions so these mistakes stop happening, without breaking questions it already gets right.
Good descriptions: say what the tool returns, when to use it, and when NOT to use it (naming the tool to use
instead); give every parameter its format and an example. Do not mention specific questions, repository names,
or benchmark details. Each description must stay under {limit} characters.

Reply with JSON only, in this shape, including only the entries you change:
{{"tools": {{"<tool name>": "<new description>"}}, "params": {{"<param name>": "<new description>"}}}}"""


def failures_text(rows: list[run_eval.Row], limit: int = 25) -> str:
    bad = [r for r in rows if r.outcome != run_eval.CORRECT]
    lines = []
    for r in bad[:limit]:
        if r.outcome == run_eval.WRONG:
            lines.append(f'- "{r.question}" -> expected {r.expected_tool}, model chose {r.actual_tool or "no tool"}')
        else:
            lines.append(f'- "{r.question}" -> right tool {r.expected_tool}, but {r.outcome.split(" ", 1)[1].lower()}: '
                         f"{r.detail[:160]} (arguments {json.dumps(r.args)})")
    return "\n".join(lines) or "(none)"


def parse_edit(text: str) -> dict[str, Any]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object in the reply")
    return json.loads(match.group(0))


def apply_edit(cfg: dict[str, Any], edit: dict[str, Any]) -> dict[str, Any]:
    new = copy.deepcopy(cfg)
    for section in ("tools", "params"):
        for key, text in (edit.get(section) or {}).items():
            if key in new[section] and isinstance(text, str) and 0 < len(text.strip()) <= MAX_DESCRIPTION:
                new[section][key] = text.strip()
    return validate_toolset(new, "candidate")


async def score(toolset_path: Path, questions: Path, args: argparse.Namespace) -> tuple[float, list[run_eval.Row]]:
    ns = argparse.Namespace(**{**vars(args), "mode": "select", "toolset": str(toolset_path),
                               "questions": str(questions), "limit": None, "markdown": False})
    rows = await run_eval.run(ns)
    return run_eval.correct_pct(rows), rows


async def optimize(args: argparse.Namespace) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    writer = run_eval.make_backend(argparse.Namespace(**{**vars(args), "backend": args.optimizer_backend,
                                                         "model": args.optimizer_model or args.model}), {})
    best = copy.deepcopy(load_toolset(args.start))
    best_path = OUT_DIR / "iter0.json"
    best_path.write_text(json.dumps(best, indent=2), encoding="utf-8")
    train = run_eval.DEFAULT_QUESTIONS["select"]
    heldout = run_eval.ROOT / "eval" / "heldout.jsonl"
    best_train, rows = await score(best_path, train, args)
    best_held, _ = await score(best_path, heldout, args)
    history = [{"iteration": 0, "train": best_train, "heldout": best_held, "accepted": True, "file": best_path.name}]

    for i in range(1, args.iterations + 1):
        if best_train == 100:
            print("\nTrain set is at 100%; nothing left to fix.")
            break
        print(f"\n=== iteration {i}: asking {args.optimizer_backend} for new descriptions")
        prompt = PROMPT.format(current=json.dumps({"tools": best["tools"], "params": best["params"]}, indent=2),
                               failures=failures_text(rows), limit=MAX_DESCRIPTION)
        try:
            candidate = apply_edit(best, parse_edit(await writer.complete(prompt)))
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"    unusable reply ({exc}); skipping")
            history.append({"iteration": i, "train": None, "heldout": None, "accepted": False, "file": None})
            continue
        path = OUT_DIR / f"iter{i}.json"
        path.write_text(json.dumps(candidate, indent=2), encoding="utf-8")
        cand_train, cand_rows = await score(path, train, args)
        cand_held, _ = await score(path, heldout, args)
        accepted = cand_train > best_train and cand_held >= best_held
        history.append({"iteration": i, "train": cand_train, "heldout": cand_held, "accepted": accepted,
                        "file": path.name})
        if accepted:
            best, best_path, best_train, best_held, rows = candidate, path, cand_train, cand_held, cand_rows

    (OUT_DIR / "best.json").write_text(json.dumps(best, indent=2), encoding="utf-8")
    (OUT_DIR / "history.json").write_text(json.dumps(history, indent=2), encoding="utf-8")
    print("\nIteration\tTrain correct\tHeld-out correct\tKept")
    for h in history:
        fmt = lambda v: "-" if v is None else f"{v:.0f}%"  # noqa: E731
        print(f"{h['iteration']}\t{fmt(h['train'])}\t{fmt(h['heldout'])}\t{'yes' if h['accepted'] else 'no'}")
    print(f"\nBest: {best_path.name} (train {best_train:.0f}%, held-out {best_held:.0f}%) -> eval/optimized/best.json")


def main() -> None:
    run_eval.load_dotenv(run_eval.ROOT / ".env")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="v1", help="toolset to start from: v1, v1b, v2 or a .json path")
    ap.add_argument("--iterations", type=int, default=4)
    ap.add_argument("--backend", choices=["anthropic", "ollama"], default="ollama", help="model under test")
    ap.add_argument("--model")
    ap.add_argument("--optimizer-backend", choices=["anthropic", "ollama"], help="model that rewrites (default: same)")
    ap.add_argument("--optimizer-model")
    ap.add_argument("--effort", default="low", choices=["low", "medium", "high"])
    ap.add_argument("--ollama-url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--max-steps", type=int, default=8)
    ap.add_argument("--fake-github", action="store_true")
    args = ap.parse_args()
    args.optimizer_backend = args.optimizer_backend or args.backend
    _fake = run_eval.use_fake_github() if args.fake_github else None  # noqa: F841
    asyncio.run(optimize(args))


if __name__ == "__main__":
    main()
