# Windows (PowerShell) quick start

GUIDE.md uses bash. These are the same steps for PowerShell, run from the project folder.

## 1. Virtual environment and install

```powershell
cd "C:\personal development\linkekdin larping\mcp server"
py -m venv .venv                      # or: python -m venv .venv  (needs Python 3.10+)
.\.venv\Scripts\Activate.ps1
# If activation is blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned  (then retry)
python -m pip install --upgrade pip
pip install -e ".[dev,eval,plot]"
pytest -q                             # 52 offline tests, no token needed
python eval\run_eval.py --fake-github --backend oracle --toolset v2   # harness self-test: 100%
```

The eval reads and writes its files as UTF-8, so `PYTHONUTF8` is no longer required. If the emoji in
the console output look garbled, set it anyway:

```powershell
$env:PYTHONUTF8 = "1"
```

## 2. Try the benchmark with no token at all

`--fake-github` serves the benchmark repos offline, so you can run a real model right away:

```powershell
ollama pull qwen2.5:7b                # gemma2 and phi3 can't call tools; mistral can
python eval\run_eval.py --fake-github --toolset v1 --limit 10
```

## 3. Tokens (.env)

Create the two fine-grained tokens described in GUIDE.md section 1 (a read-only one for the server,
and a short-lived write one only for seeding), then:

```powershell
Copy-Item .env.example .env
notepad .env               # paste GITHUB_TOKEN and GITHUB_SEED_TOKEN
```

Check the read-only token:

```powershell
Get-Content .env | Where-Object { $_ -match '^\s*([^#=][^=]*)=(.*)$' } |
  ForEach-Object { Set-Item -Path "env:$($matches[1].Trim())" -Value $matches[2].Trim() }

$h = @{ Authorization = "Bearer $env:GITHUB_TOKEN" }
(Invoke-RestMethod -Headers $h https://api.github.com/user).login
(Invoke-RestMethod -Headers $h https://api.github.com/rate_limit).resources.core
```

## 4. Create the private benchmark repos (once)

```powershell
python eval\seed_repos.py --dry-run
python eval\seed_repos.py
```

Then set `EVAL_REPO=<you>/mcp-bench-app` and `EVAL_REPO2=<you>/mcp-bench-lib` in `.env`, wait a minute,
and delete `GITHUB_SEED_TOKEN` (on GitHub and in `.env`).

## 5. Run the evaluation

```powershell
python eval\run_eval.py --toolset v1  --runs 3
python eval\run_eval.py --toolset v1b --runs 3
python eval\run_eval.py --toolset v2  --runs 3
python eval\run_eval.py --toolset v2  --runs 3 --questions eval\heldout.jsonl
python eval\run_eval.py --mode agent --toolset v2
python eval\optimize.py --start v1b --iterations 4
python eval\plot_results.py (Get-ChildItem eval\results\select-*.jsonl).FullName -o docs\results.png
```

For the Claude backend, set `$env:ANTHROPIC_API_KEY = "sk-ant-..."` (or put it in `.env`) and add
`--backend anthropic`.

## 6. Inspector and Claude Desktop

```powershell
npx @modelcontextprotocol/inspector .\.venv\Scripts\github-mcp.exe
```

Claude Desktop config is `%APPDATA%\Claude\claude_desktop_config.json` (Settings > Developer > Edit Config):

```json
{
  "mcpServers": {
    "github": {
      "command": "C:\\personal development\\linkekdin larping\\mcp server\\.venv\\Scripts\\github-mcp.exe",
      "env": {
        "GITHUB_TOKEN": "github_pat_xxxxxxxxxxxxxxxxxxxx",
        "GITHUB_MCP_TOOLSET": "v2"
      }
    }
  }
}
```

Quit Claude Desktop from the system tray (not just the window) and reopen it. Logs are in
`%APPDATA%\Claude\logs\mcp-server-github.log`.

## 7. Publish to GitHub

```powershell
git init
git add .
git status                 # confirm .env is NOT listed
git commit -m "GitHub MCP server with tool-selection benchmark"
gh repo create github-mcp --public --source . --push     # or create it on github.com and git push
```
