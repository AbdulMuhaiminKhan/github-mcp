#!/usr/bin/env python3
"""Export the published results to docs/data.js for the results dashboard (docs/index.html).

  python eval/export_dashboard.py

Reads every results file directly in eval/results/ (not offline/ or archive/), keeps the newest file
per (mode, toolset, model, question set), and writes one JS file the static page loads. Open
docs/index.html in a browser, or serve docs/ with GitHub Pages.
"""

from __future__ import annotations

import json
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import run_eval  # noqa: E402

from github_mcp.descriptions import load_toolset  # noqa: E402

ROOT = run_eval.ROOT
OUT = ROOT / "docs" / "data.js"
KEYS = {run_eval.CORRECT: "correct", run_eval.WRONG_ARGS: "wrong_args", run_eval.WRONG: "wrong_tool",
        run_eval.FAILED: "failed", run_eval.ANSWER_OK: "correct", run_eval.ANSWER_WRONG: "wrong_answer",
        run_eval.NO_ANSWER: "no_answer"}
TOOLSET_ORDER = {"v1": 0, "v1b": 1, "v2": 2}


def latest_files(results: Path) -> list[Path]:
    newest: dict[tuple[str, ...], Path] = {}
    for path in sorted(results.glob("*.jsonl")):  # names end in a timestamp, so sorted = oldest first
        rows = run_eval.read_rows(path)
        if not rows or not rows[0].split:
            continue
        r = rows[0]
        newest[(r.mode, r.toolset, r.model, r.split)] = path
    return list(newest.values())


def summarize(path: Path) -> dict[str, Any]:
    rows = run_eval.read_rows(path)
    runs = run_eval.by_run(rows)
    per_run = len(next(iter(runs.values())))
    r0 = rows[0]
    outcomes = run_eval.outcomes_for(rows)
    pct = {KEYS[o]: statistics.median(100 * sum(r.outcome == o for r in rr) / per_run for rr in runs.values())
           for o in outcomes}
    tokens = [r.input_tokens for r in rows if r.input_tokens]
    cells: dict[str, dict[str, Any]] = {}
    for qid, outcome in run_eval.majority(rows).items():
        same = [r for r in rows if r.id == qid]
        example = next(r for r in same if r.outcome == outcome)
        cells[qid] = {
            "outcome": KEYS[outcome], "agree": sum(r.outcome == outcome for r in same), "runs": len(same),
            "tool": example.actual_tool, "args": example.args, "detail": example.detail[:400],
            "answer": example.answer, "calls": [c["tool"] for c in example.tool_calls],
        }
    confusions = Counter((r.expected_tool, r.actual_tool or "(no tool)") for r in rows if r.outcome == run_eval.WRONG)
    return {
        "key": f"{r0.mode}|{r0.model}|{r0.toolset}|{r0.split}",
        "mode": r0.mode, "model": r0.model, "toolset": r0.toolset, "split": r0.split,
        "data": r0.data or "github", "runs": len(runs), "n": per_run, "pct": pct,
        "tokens_in": round(statistics.mean(tokens)) if tokens else None,
        "latency_s": round(statistics.mean(r.latency_s for r in rows), 2),
        "confusions": [{"expected": e, "chosen": a, "count": n} for (e, a), n in confusions.most_common()],
        "file": path.relative_to(ROOT).as_posix(),
        "cells": cells,
        "questions": {r.id: {"question": r.question, "category": r.category, "expected_tool": r.expected_tool}
                      for r in rows if r.run == 0},
    }


def descriptions() -> dict[str, Any]:
    out = {name: load_toolset(name) for name in ("v1", "v2")}
    best = ROOT / "eval" / "optimized" / "best.json"
    if best.is_file():
        out["optimized"] = load_toolset(str(best))
    return {k: {"tools": v["tools"], "params": v["params"], "bare_names": v["allow_bare_repo_name"]} for k, v in out.items()}


def main() -> None:
    configs = [summarize(p) for p in latest_files(ROOT / "eval" / "results")]
    configs.sort(key=lambda c: (c["mode"] != "select", c["model"], c["split"] != "questions",
                                TOOLSET_ORDER.get(c["toolset"], 9), c["toolset"]))
    questions: dict[str, dict[str, Any]] = defaultdict(dict)
    for c in configs:
        for qid, q in c.pop("questions").items():
            questions[c["split"]].setdefault(qid, q)
    data = {"generated": time.strftime("%Y-%m-%d"), "configs": configs,
            "questions": {split: [{"id": k, **v} for k, v in sorted(qs.items())] for split, qs in questions.items()},
            "descriptions": descriptions()}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("window.BENCH = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n",
                   encoding="utf-8")
    print(f"Wrote {OUT.relative_to(ROOT)}: {len(configs)} configurations, "
          f"{sum(len(q) for q in data['questions'].values())} questions")


if __name__ == "__main__":
    main()
