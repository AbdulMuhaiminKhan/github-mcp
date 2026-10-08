# Project review: GitHub MCP Server

Reviewed 2026-10-07. The 9 offline tests pass (checked in a fresh Python venv). The eval has
**not been run yet**, so this review has no measured accuracy numbers. Anything below about
accuracy is a prediction until you run it.

---

## Update: every recommendation is now implemented

Later on 2026-10-07 all of Part 4 was built. Parts 1 to 4 below are the original review of version 1.0,
kept for the record. GUIDE.md and README.md describe the current version (1.1). Tests went from 9 to 52.
The eval itself still needs a model run on your laptop to produce real numbers.

| # | Recommendation | What was done | Where |
|---|---|---|---|
| 1 | Held-out set | 20 new questions, never used for tuning; the optimizer can't see them either | `eval/heldout.jsonl` |
| 2 | Ablation | `v1b` toolset (v1 text + bare names). Measured with the oracle: v1 alone loses 17 points to bare names | `descriptions.py` |
| 3 | Repeat runs | `--runs N` reports medians and the range; `--compare` uses the majority outcome per question | `run_eval.py` |
| 4 | Score arguments | `expected_args` on every question and a new 🟡 Wrong Arguments outcome | `grading.py`, question files |
| 5 | Publish properly | Real README, MIT LICENSE, pyproject metadata, results no longer gitignored, `__pycache__` removed | repo root |
| 6 | Counts capped by limit | List tools return exact totals (`total`, `total_open`, `total_is_exact`) | `server.py` |
| 7 | Fewer issues than asked | Issues are paged until complete (up to 500), PRs dropped | `server.py` |
| 8 | No pagination | Shared `collect()` pager following GitHub's Link headers | `github_client.py` |
| 9 | Pasted repo URLs | `http(s)://`, `www.`, `github.com/o/n` and `.git` all resolve | `github_client.py` |
| 10 | Auto-optimizer | Rewrites from train failures, keeps a change only if held-out doesn't drop | `eval/optimize.py` |
| 11 | Results chart | 100% stacked bars per configuration, plus a Markdown table | `eval/plot_results.py` |
| 12 | Eval in CI | Oracle self-test on every push; a Claude Haiku eval on description changes when a secret is set | `.github/workflows/` |
| 13 | Cost and latency | Mean input/output tokens and latency per question in every report | `run_eval.py` |
| 14 | More tools | `list_pull_requests` (6th tool) and 10 PR questions, so 60 in total | `server.py`, `questions.jsonl` |
| 15 | Multi-turn eval | `--mode agent`: answers checked against the seeded data | `run_eval.py`, `agent_questions.jsonl` |
| 16 | Packaging | Dockerfile, `uvx` instructions, ETag caching (304s are free) | `Dockerfile`, `github_client.py` |
| T4 | Hygiene | ruff + mypy in CI, Python 3.10–3.13 matrix, tests for every tool and the harness, client closed on shutdown, q03/q24/q32 reworded | various |

Added beyond the list, because you asked for benchmark repos:
- **`eval/seed_repos.py`** creates two private repos (`mcp-bench-app`, `mcp-bench-lib`) in your
  account with 15 issues, 5 PRs (open, draft, merged, closed) and commits from two authors.
  It needs a separate short-lived token with write access; see GUIDE.md section 1.
- **`eval/fake_github.py`** serves the same repos offline, so `--fake-github` runs the whole
  benchmark with no token, and CI can check the harness on every push.
- The Ollama backend now stops with a clear message if the model can't call tools (gemma2, phi3).

Not verified here: the Docker image (no Docker daemon in this environment; the same install steps were
tested with pip) and the seed script against real GitHub (tested against a mock of the API).

---

## Part 1: What this project is, in plain language

### The one-sentence pitch
You built a small server that lets an AI assistant (Claude, or a local model) answer questions
about *your* GitHub account, and then you **measured** how well the AI picks the right tool,
and **improved** that number by rewriting the tool descriptions.

The second half (measure, then improve) is the part that makes this a portfolio project and not
a tutorial. Most people stop at "I built an MCP server". You'll have a benchmark and a before/after.

### What MCP is
MCP (Model Context Protocol) is a standard plug for AI apps. An "MCP server" exposes **tools**
(functions with a name, a description, and typed inputs). An AI app such as Claude Desktop reads
the list of tools, and when you ask a question, the model decides which tool to call and with
what arguments. The app runs the call and gives the result back to the model.

The model never sees your code. It only sees the **name, description and parameter schema**.
That is why descriptions matter so much, and it is the whole thesis of this project.

### The pieces

| File | What it does |
|---|---|
| `src/github_mcp/github_client.py` | Talks to the GitHub REST API. Handles auth, retries on server errors (with backoff and jitter), rate limits (waits up to 10s, otherwise fails fast with the reset time), and turns every HTTP error into a readable message that tells the model what to do next ("Call list_repositories to check the exact name"). |
| `src/github_mcp/server.py` | The MCP server. Registers 5 read-only tools, validates inputs with Pydantic (e.g. `limit` must be 1 to 50), trims GitHub's huge JSON down to the fields that matter, and converts errors into MCP tool errors instead of crashing. Logs go to stderr because stdout is the protocol channel. |
| `src/github_mcp/descriptions.py` | The experiment's two "treatments". **v1**: deliberately vague ("Gets issues."). **v2**: each tool says what it returns, when to use it, and when NOT to use it (naming the alternative). v2 also flips one behaviour flag: it accepts bare repo names like `my-app`. |
| `eval/questions.jsonl` | 50 questions, 10 per tool, tagged by category: `direct`, `overlap:<other tool>` (deliberately ambiguous), `vague`, and `bare_name`. |
| `eval/run_eval.py` | The benchmark. Starts the real server, gives its tool list to an LLM (local Ollama for $0, or Claude via the API), records which tool the model picked, executes it against real GitHub, and scores **Correct / Wrong Tool / Tool Failed**. Also prints a confusion list and can `--compare` two runs to show fixed and regressed questions. |
| `tests/test_server.py` | 9 offline tests using a fake GitHub (no token needed): PRs excluded from issues, bare-name handling v1 vs v2, 404/401/rate-limit messages, schema validation, retry on 502, search query construction. |
| `GUIDE.md`, `README_TEMPLATE.md`, `WINDOWS.md` | How to set up, run, and present it. The README template has `[brackets]` waiting for your real numbers. |
| `.github/workflows/test.yml` | CI that runs the tests on every push. |

### The five tools

| Tool | GitHub endpoint | Answers questions like |
|---|---|---|
| `list_repositories` | `GET /user/repos` | "Which repos do I have?", "Which has the most stars?" |
| `get_repository` | `GET /repos/{owner}/{name}` | "What license does X use?", "How many forks?" |
| `list_open_issues` | `GET /repos/{o}/{n}/issues?state=open` (PRs filtered out) | "What's open in X?", "Open bugs?" |
| `search_issues` | `GET /search/issues` | "Issues mentioning auth anywhere?", "Closed issues?" |
| `get_recent_commits` | `GET /repos/{o}/{n}/commits` | "What changed this week?", "Who committed last?" |

All are read-only and marked with `readOnlyHint`, and the guide tells you to use a fine-grained,
read-only token. That's a good security story to mention in interviews.

### How a single eval question flows
1. Question: *"How many open issues does me/app have?"* (category `overlap:get_repository`, because
   `get_repository` returns `open_issues_count`, which wrongly includes PRs).
2. The harness sends the question plus the server's tool list to the model.
3. Model calls, say, `get_repository`. That isn't the expected `list_open_issues`, so the
   question is scored **Wrong Tool**.
4. If it had picked `list_open_issues`, the harness runs the call against GitHub. Error means
   **Tool Failed**, success means **Correct**.

### What the code does well (say these out loud in interviews)
- **Errors are written for the model**, with a next step. That's a real agent-design idea, not boilerplate.
- **Honest data handling**: PRs are excluded from issues; `get_repository` renames the misleading
  field to `open_issues_and_prs`.
- **Resilience**: retry with exponential backoff and jitter, primary and secondary rate-limit handling.
- **Testable by design**: the HTTP transport is injectable, so tests run offline in about 1 second.
- **The eval drives the real server over stdio**, not a mock, so it measures what a user would get.
- **The v1/v2 split is a clean A/B design**, and `--compare` reports regressions, not just the headline.

---

## Part 2: Where you are and what's next to get numbers

On your laptop: setup done, tests pass. To get the first real results:

1. **Put a real token and repos in `.env`** (`GITHUB_TOKEN`, `EVAL_REPO`, `EVAL_REPO2`). Pick repos
   that actually have some open issues, some closed issues, and recent commits, otherwise several
   questions are trivially easy or meaningless.
2. **Pick a local model that can call tools.** Of the models you already have, **only `mistral`
   supports tool calling in Ollama. `gemma2:9b` and `phi3` do not**, so they'd score "Wrong Tool" on
   nearly everything (or error out). Either run `ollama pull qwen2.5:7b` (the default the harness expects),
   or start with `--model mistral`. `llama3.1:8b` and `qwen3:8b` are also good options.
3. Run v1, run v2, then `python eval\run_eval.py --compare <v1 file> <v2 file>`.
4. Paste the real numbers into the README. Don't use the 64/22/14 to 92% figures from the guide; they
   are illustrative targets, and an interviewer will ask how you measured.

---

## Part 3: What I fixed

**`eval/run_eval.py` now reads and writes files as UTF-8.** It previously used the system default
encoding. On Windows that's usually cp1252, which can't encode the ✅ ⚠️ ❌ outcome labels, so saving
results would crash unless `PYTHONUTF8=1` was set. Four call sites changed, nothing else.
Your laptop copy doesn't have this fix yet. Keep setting `PYTHONUTF8=1` there (printing emoji to a
redirected console can still need it), or copy the updated file over.

Nothing else was changed. Everything below is a recommendation.

---

## Part 4: Improvements, prioritized

Ordered by **impact on how a recruiter or hiring engineer reads the project**, then by effort.

### Tier 1: Do these before you publish (credibility)

**1. Add a held-out test set. This is the most important one.**
The v2 descriptions were written while looking at these same 50 questions. Any score jump on the
same questions is partly "teaching to the test". Your README template already promises a "held-out
set (10 questions written after tuning)", but **that file doesn't exist and the harness has no
option for it**. Write 15 to 20 *new* questions (different wording, same tools) into
`eval/heldout.jsonl` before you look at v2 results, and report both scores. A reviewer who knows
evals will look for exactly this. *Effort: 30 minutes, since `--questions` already accepts a path.*

**2. Separate the two effects in v2 (an ablation).**
v2 changes two things at once: the description text **and** the `allow_bare_repo_name` flag. The
flag alone turns every `bare_name` question (8 of 50) from a guaranteed v1 failure into a likely
pass. That's up to 16 points of "improvement" that has nothing to do with descriptions. A sharp
reader will call it a rigged baseline. Fix: add a `v1b` toolset (v1 text + bare names on) and report
v1 → v1b → v2. Then you can honestly say "descriptions gave X points, input handling gave Y points".
*Effort: 10 lines in `descriptions.py` and adding `v1b` to the `--toolset` choices.*

**3. Run each configuration several times, on more than one model.**
One run at temperature 0 on one model is a single anecdote. With 50 questions, each question is
2 points. The README template already says "3 runs, median", but the harness has no repeat option.
Add `--runs N` and report the median and range. Show at least two models (e.g. qwen2.5:7b and
mistral locally, and Claude once if you can spend about $1). The finding "descriptions matter a lot
for small local models and little for frontier models" is itself interesting and true to the guide.
*Effort: about 1 hour.*

**4. Score the arguments, not just the tool name.**
Right now "Correct" means *right tool and GitHub didn't error*. So `"Show me commits from the last 3
days"` scores Correct even if the model forgot `since_days=3`, and `"List open issues labeled
'enhancement'"` scores Correct without the label. Add an optional `expected_args` field to each
question (e.g. `{"since_days": 3}`, `{"label": "enhancement"}`, `{"state": "closed"}`) and a fourth
bucket, **Wrong Arguments**. This makes the benchmark noticeably more rigorous and gives you a
second axis to optimize (parameter descriptions). *Effort: 1 to 2 hours.*

**5. Publish it properly.**
It isn't on GitHub yet. When you push it:
- Rename `README_TEMPLATE.md` to `README.md` and fill in every `[bracket]` with measured numbers.
- Add a `LICENSE` (MIT). `pyproject.toml` has no license or author fields either.
- Commit the results JSONL files. Note that `.gitignore` currently ignores `eval/results/*.jsonl`,
  which contradicts the guide's advice to commit them. Remove that line, or commit them with `git add -f`.
- Never commit `.env` or a filled-in `claude_desktop_config.json` (it holds your token in plain text).
- Add the CI badge, a 30-second demo GIF (ScreenToGif on Windows), and repo topics (`mcp`,
  `llm-evaluation`, `tool-use`).

### Tier 2: Correctness fixes (small, real bugs in tool behaviour)

**6. Counts are capped by `limit`, but the model will read them as totals.**
`list_open_issues` returns `"count": len(issues)` with a default limit of 20. A repo with 35 open
issues reports `count: 20`, and the model will confidently answer "20". Question q22 ("How many open
issues does X have?") asks exactly this. `list_repositories` has the same issue for "How many private
repos do I own?" (max 50). Fix: rename the field to `returned`, add `has_more: true` when the page
was full, or fetch the true total (the search API returns `total_count` for
`repo:X is:issue is:open`). *Effort: about 30 minutes plus a test.*

**7. `list_open_issues` can return fewer issues than asked for.**
It fetches `limit × 2` items and drops PRs. A busy repo where most open items are PRs might return
3 issues when 20 exist. Either page until you have `limit` issues, or use the search API with
`is:issue`, which excludes PRs server-side.

**8. No pagination anywhere.** Everything is a single page (max 50). That's fine for a demo, but
mention it under trade-offs in the README, or add a `page` or `cursor` parameter.

**9. Small input-handling gaps.** `resolve_repo` strips `https://github.com/` but not `http://`,
`www.`, or `github.com/owner/name` without a scheme. That's a one-line regex change.

### Tier 3: Features that make it stand out

Pick one or two. Depth beats breadth.

**10. Automatic description optimization (the strongest "wow" option).**
Write a script that takes the v1 confusion list, asks an LLM to rewrite the conflicting
descriptions, re-runs the eval, and keeps changes that improve the **held-out** score. That's a
small version of what tool-description optimizers in industry do, and it turns "I hand-tuned some
text" into "I built an optimization loop with a guard against overfitting". It needs items 1 and 3 first.

**11. A results chart in the README.** A grouped bar chart (v1 / v1b / v2 × model) or a
tool-confusion heatmap. Recruiters skim; one image carries the whole story. Generate it from the
JSONL with matplotlib so it's reproducible.

**12. Eval in CI.** Run a 10-question smoke eval on every PR that changes `descriptions.py`, and fail
if accuracy drops. Your README already lists this as "next". Doing it shows MLOps thinking. (It needs
a token in GitHub Secrets and a cheap model, or a recorded-response mode.)

**13. Cost and latency columns.** The harness already records latency per question. Add input
tokens (v2 descriptions are longer, so every request costs more) and report the accuracy versus
token-cost trade-off. The README template has a placeholder for this.

**14. More realistic tools.** Add `list_pull_requests` (PR questions currently have no correct tool),
or a `get_file_contents`/README tool. Adding a tool that deliberately overlaps (PRs vs issues) is a
nice second experiment: "does v2-style writing scale to 6 tools?"

**15. Multi-turn eval.** The current eval is single-shot: pick one tool. Real use is multi-step
("which of my repos has the most open issues?" needs list then several gets). A small multi-turn
mode that checks the *final answer* against ground truth would make the project much more
advanced. This is the most effort of anything listed.

**16. Packaging polish.** Make it installable with `uvx github-mcp`, add a Dockerfile, and
optionally list it in the MCP registry. Use ETag conditional requests (304 responses don't count
against GitHub's rate limit) as a small performance feature to talk about.

### Tier 4: Code hygiene (quick wins, low visibility)

- Add `ruff` (lint and format) and `mypy` to CI, and test on Python 3.10 to 3.13, since
  `pyproject.toml` claims `>=3.10` but CI only tests 3.12.
- Add tests for `get_recent_commits` and `list_repositories` (currently untested) and for the
  eval's `report()` and `compare()` functions.
- `GitHubClient.aclose()` is never called. That's harmless for a stdio process, but tidy it with a
  lifespan hook.
- Remove the committed `__pycache__` folders before pushing (`.gitignore` already covers them).
- Benchmark labels to double-check before you publish: q03 ("worked on most recently") and q24
  ("what should I work on next") are arguably ambiguous, and q32 ("issues *I* closed") can't
  actually filter by who closed them. Either re-word them or note them as known-ambiguous.

---

## Part 5: Suggested order of work

1. Pull a tool-capable model, fill `.env`, run v1 and v2 once. **This gets you real numbers today.**
2. Write the held-out questions (#1) and add the `v1b` ablation (#2).
3. Add `--runs` (#3) and argument scoring (#4), then re-run everything on two models.
4. Fix the count bug (#6) and add a test for it.
5. Publish (#5) with real numbers, a chart (#11), and a GIF.
6. If you want a standout extra: the auto-optimizer loop (#10) or eval-in-CI (#12).

### How to talk about it (once you have numbers)
> "I built a read-only MCP server over the GitHub API, then built a benchmark to measure how often
> the model picks the right tool. Vague descriptions scored X% on a local 7B model. Rewriting them
> as mutually exclusive contracts took it to Y% on a held-out set. I separated the description effect
> from an input-handling fix with an ablation, and the remaining errors were mostly Z."

Every number in that paragraph should come from a committed results file.
