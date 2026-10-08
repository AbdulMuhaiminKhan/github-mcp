#!/usr/bin/env python3
"""Chart eval results for the README: one 100% stacked bar per configuration.

  python eval/plot_results.py eval/results/select-*.jsonl -o docs/results.png

Each bar is one results file (toolset · model · question set); segments are the outcome shares
(medians across runs). Also prints the same numbers as a Markdown table to paste under the chart.
Needs matplotlib:  pip install -e '.[plot]'
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_eval import SELECT_OUTCOMES, by_run, read_rows  # noqa: E402

# Status palette: outcomes are states (good -> critical), and every segment is also named in the legend.
COLORS = ["#0ca30c", "#fab219", "#ec835a", "#d03b3b"]
SURFACE, INK, INK_2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
TOOLSET_ORDER = {"v1": 0, "v1b": 1, "v2": 2}


def shares(path: Path) -> tuple[str, list[float], int]:
    rows = read_rows(path)
    if not rows or rows[0].mode != "select":
        raise SystemExit(f"{path.name}: only select-mode results can be charted")
    runs = by_run(rows)
    per_run = len(next(iter(runs.values())))
    pct = [statistics.median(100 * sum(r.outcome == s for r in rr) / per_run for rr in runs.values())
           for s in SELECT_OUTCOMES]
    r = rows[0]
    split = "held-out" if r.split == "heldout" else f"{per_run} questions"
    return f"{r.toolset} · {r.model} · {split}", pct, len(runs)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("-o", "--out", type=Path, default=Path("docs/results.png"))
    ap.add_argument("--title", default="Tool-selection accuracy by description set")
    args = ap.parse_args()

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    data = [shares(f) for f in args.files]
    data.sort(key=lambda d: (d[0].split(" · ")[1], TOOLSET_ORDER.get(d[0].split(" · ")[0], 9), d[0]))
    labels = [d[0] for d in data]

    fig, ax = plt.subplots(figsize=(9, 0.55 * len(data) + 1.6), dpi=160)
    fig.patch.set_facecolor(SURFACE)
    ax.set_facecolor(SURFACE)
    y = list(range(len(data)))[::-1]
    left = [0.0] * len(data)
    for k, (status, color) in enumerate(zip(SELECT_OUTCOMES, COLORS)):
        widths = [d[1][k] for d in data]
        ax.barh(y, widths, left=left, height=0.56, color=color, edgecolor=SURFACE, linewidth=2,
                label=status.split(" ", 1)[1])
        left = [a + b for a, b in zip(left, widths)]
    for yi, d in zip(y, data):  # direct label: the headline number only
        ax.text(101.5, yi, f"{d[1][0]:.0f}% correct", va="center", ha="left", fontsize=9, color=INK)

    ax.set_yticks(y, labels, fontsize=9, color=INK)
    ax.set_xlim(0, 100)
    ax.set_xticks(range(0, 101, 25), [f"{v}%" for v in range(0, 101, 25)], fontsize=8, color=INK_2)
    ax.xaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(length=0)
    ax.set_title(args.title, loc="left", fontsize=11, color=INK, pad=26)
    ax.legend(ncol=4, loc="lower left", bbox_to_anchor=(0, 1.0), frameon=False, fontsize=8.5,
              labelcolor=INK_2, handlelength=1.2, borderaxespad=0.2)
    fig.subplots_adjust(right=0.86)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(args.out, bbox_inches="tight", facecolor=SURFACE)
    print(f"Saved {args.out}\n")

    head = ["Configuration"] + [s.split(" ", 1)[1] for s in SELECT_OUTCOMES] + ["Runs"]
    print("| " + " | ".join(head) + " |\n|" + "---|" * len(head))
    for label, pct, runs in data:
        print(f"| {label} | " + " | ".join(f"{p:.0f}%" for p in pct) + f" | {runs} |")


if __name__ == "__main__":
    main()
