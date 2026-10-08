# Results

Every number in the README comes from a file in this folder (one JSON row per question per run).

- `*.jsonl` here: the published runs (qwen2.5:7b and llama3.1:8b), against the seeded benchmark repos on real GitHub.
- `offline/`: the same configurations run against the offline fake GitHub (`--fake-github`).
  They agree with the real-GitHub runs within 2 points.
- `offline/` also holds the description optimizer's held-out check (`select-best-...`); the optimizer's
  iterations and history are in `../optimized/`. It kept nothing, so its best is v1.
- `archive/`: a 10-question smoke test and v1.0 runs from before argument grading existed.

Re-print any summary with `python eval/run_eval.py --report <file> --markdown`, and rebuild the
dashboard data with `python eval/export_dashboard.py`.
