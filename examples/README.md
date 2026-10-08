# Benchmark your own MCP server

`eval/run_eval.py` is not tied to the GitHub server. Give it the command that starts any MCP server
over stdio and a JSONL file of questions, and it measures how often a model picks the right tool with
the right arguments.

```bash
# self-test: the oracle always picks the expected call, so this should print 100%
python eval/run_eval.py --server "python examples/notes_server.py" \
    --questions examples/notes_questions.jsonl --backend oracle

# a real model, free and local
python eval/run_eval.py --server "python examples/notes_server.py" \
    --questions examples/notes_questions.jsonl --domain "the user's notebook" \
    --backend ollama --model qwen2.5:7b --runs 3

# someone else's server, e.g. the reference filesystem server
python eval/run_eval.py --server "npx -y @modelcontextprotocol/server-filesystem ." \
    --questions my_fs_questions.jsonl --domain "the user's files"
```

## Question format

One JSON object per line:

```json
{"id": "n06", "question": "Which note mentions caching?", "expected_tool": "search_notes",
 "category": "direct", "expected_args": {"query": {"contains": "caching"}}}
```

`expected_args` maps parameter names to an exact value, `null` (must be absent),
`{"contains": "s"}`, `{"any": [a, b]}` or `{"range": [lo, hi]}`. Parameters you leave out are not graded.
Put a third of your questions where two tools overlap: that is where descriptions make the difference.

Each call is graded ✅ Correct, 🟡 Wrong Arguments, ⚠️ Wrong Tool or ❌ Tool Failed, and the report lists
which tools the model confuses. `--report` and `--compare` work on these results files too.
