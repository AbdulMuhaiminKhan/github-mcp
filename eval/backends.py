"""LLM backends for the eval: Claude (Anthropic API), a local model (Ollama), and an oracle.

Each backend can
  choose(q)                  single shot: which tool, with which arguments, for one question
  run_agent(q, call_tool)    multi-turn: call tools until it can answer, then answer in text
  complete(prompt)           plain text completion (used by optimize.py to rewrite descriptions)
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

SYSTEM_PROMPT = (
    "You are an assistant connected to the user's GitHub account through tools. "
    "Answer the user's question by calling the single most appropriate tool."
)
AGENT_SYSTEM_PROMPT = (
    "You are an assistant connected to the user's GitHub account through tools. "
    "Use the tools to find the facts you need, then answer the user's question in one or two "
    "sentences. Base the answer only on tool results."
)

ToolCaller = Callable[[str, dict[str, Any]], Awaitable[tuple[str, bool]]]  # -> (result text, is_error)


@dataclass
class Choice:
    tool: str | None
    args: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass
class AgentResult:
    answer: str | None
    calls: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


class AnthropicBackend:
    """Claude picks a tool. tool_choice stays 'auto': not calling a tool counts as a wrong pick."""

    name = "anthropic"

    def __init__(self, model: str, effort: str):
        import anthropic  # optional dependency: pip install -e '.[eval]'

        self.client = anthropic.AsyncAnthropic(max_retries=4)
        self.model, self.effort = model, effort

    def set_tools(self, mcp_tools: list[Any]) -> None:
        self.tools = [
            {"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
            for t in mcp_tools
        ]

    def _extra(self) -> dict[str, Any]:
        """Model-dependent options: effort isn't accepted by Haiku 4.5; refusal fallbacks exist
        only on the newest models (server-side, so a rare refusal doesn't break the run)."""
        extra: dict[str, Any] = {}
        if not self.model.startswith("claude-haiku"):
            extra["output_config"] = {"effort": self.effort}
        if self.model.startswith(("claude-opus-5", "claude-fable", "claude-sonnet-5-5")):
            extra["betas"] = ["server-side-fallback-2026-07-01"]
            extra["fallbacks"] = "default"
        return extra

    async def _create(self, system: str, messages: list[dict[str, Any]], tools: bool = True) -> Any:
        return await self.client.beta.messages.create(
            model=self.model, max_tokens=8192, system=system, messages=messages,
            **({"tools": self.tools} if tools else {}), **self._extra(),
        )

    async def choose(self, q: dict[str, Any]) -> Choice:
        resp = await self._create(SYSTEM_PROMPT, [{"role": "user", "content": q["question"]}])
        usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
        if resp.stop_reason == "refusal":
            return Choice(None, note="model refused", **usage)
        for block in resp.content:
            if block.type == "tool_use":
                return Choice(block.name, dict(block.input), **usage)
        text = " ".join(b.text for b in resp.content if b.type == "text")
        return Choice(None, note=f"no tool call: {text[:160]}", **usage)

    async def run_agent(self, q: dict[str, Any], call_tool: ToolCaller, max_steps: int = 8) -> AgentResult:
        messages: list[dict[str, Any]] = [{"role": "user", "content": q["question"]}]
        result = AgentResult(None)
        for _ in range(max_steps):
            resp = await self._create(AGENT_SYSTEM_PROMPT, messages)
            result.input_tokens += resp.usage.input_tokens
            result.output_tokens += resp.usage.output_tokens
            if resp.stop_reason == "refusal":
                result.note = "model refused"
                return result
            # Append the full content unchanged (thinking blocks included), then all tool results
            # in one user message.
            messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
            uses = [b for b in resp.content if b.type == "tool_use"]
            if not uses:
                result.answer = " ".join(b.text for b in resp.content if b.type == "text").strip()
                return result
            tool_results = []
            for b in uses:
                text, is_error = await call_tool(b.name, dict(b.input))
                result.calls.append({"tool": b.name, "args": dict(b.input), "error": is_error})
                tool_results.append({"type": "tool_result", "tool_use_id": b.id, "content": text, "is_error": is_error})
            messages.append({"role": "user", "content": tool_results})
        result.note = f"no answer after {max_steps} steps"
        return result

    async def complete(self, prompt: str) -> str:
        resp = await self._create("You improve tool descriptions for an LLM tool-use benchmark.",
                                  [{"role": "user", "content": prompt}], tools=False)
        return " ".join(b.text for b in resp.content if b.type == "text")


class OllamaBackend:
    """A local model through Ollama (https://ollama.com). Free; needs a tool-calling model."""

    name = "ollama"

    def __init__(self, model: str, url: str):
        self.model, self.url = model, url.rstrip("/")
        self.http = httpx.AsyncClient(timeout=300)

    def set_tools(self, mcp_tools: list[Any]) -> None:
        self.tools = [
            {"type": "function",
             "function": {"name": t.name, "description": t.description or "", "parameters": t.input_schema}}
            for t in mcp_tools
        ]

    async def _chat(self, messages: list[dict[str, Any]], tools: bool = True, fmt: str | None = None) -> dict[str, Any]:
        resp = await self.http.post(f"{self.url}/api/chat", json={
            "model": self.model, "stream": False, "options": {"temperature": 0},
            "messages": messages, **({"tools": self.tools} if tools else {}), **({"format": fmt} if fmt else {}),
        })
        if resp.status_code == 400 and "does not support tools" in resp.text:
            raise SystemExit(f"Ollama model {self.model!r} does not support tool calling. "
                             "Use one that does, e.g. qwen2.5:7b, llama3.1:8b or mistral.")
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def _args(fn: dict[str, Any]) -> dict[str, Any]:
        args = fn.get("arguments") or {}
        return json.loads(args or "{}") if isinstance(args, str) else args

    async def choose(self, q: dict[str, Any]) -> Choice:
        data = await self._chat([{"role": "system", "content": SYSTEM_PROMPT},
                                 {"role": "user", "content": q["question"]}])
        usage = {"input_tokens": data.get("prompt_eval_count"), "output_tokens": data.get("eval_count")}
        msg = data["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            return Choice(None, note=f"no tool call: {msg.get('content', '')[:160]}", **usage)
        fn = calls[0]["function"]
        return Choice(fn["name"], self._args(fn), **usage)

    async def run_agent(self, q: dict[str, Any], call_tool: ToolCaller, max_steps: int = 8) -> AgentResult:
        messages: list[dict[str, Any]] = [{"role": "system", "content": AGENT_SYSTEM_PROMPT},
                                          {"role": "user", "content": q["question"]}]
        result = AgentResult(None)
        for _ in range(max_steps):
            data = await self._chat(messages)
            result.input_tokens += data.get("prompt_eval_count") or 0
            result.output_tokens += data.get("eval_count") or 0
            msg = data["message"]
            messages.append(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                result.answer = (msg.get("content") or "").strip()
                return result
            for c in calls:
                fn = c["function"]
                args = self._args(fn)
                text, is_error = await call_tool(fn["name"], args)
                result.calls.append({"tool": fn["name"], "args": args, "error": is_error})
                messages.append({"role": "tool", "tool_name": fn["name"], "content": text})
        result.note = f"no answer after {max_steps} steps"
        return result

    async def complete(self, prompt: str) -> str:
        data = await self._chat([{"role": "user", "content": prompt}], tools=False, fmt="json")
        return data["message"].get("content", "")


class OracleBackend:
    """Always picks the expected tool with arguments that satisfy expected_args.

    Not a model: it tests the harness, the server and GitHub (or the fake) end to end. A perfect
    oracle score means every question is answerable; anything below 100% is a harness or data bug
    (or, for v1, the bare-name questions the v1 server rejects by design).
    """

    name = "oracle"

    def __init__(self, subs: dict[str, str]):
        self.subs = subs

    def set_tools(self, mcp_tools: list[Any]) -> None:
        pass

    async def choose(self, q: dict[str, Any]) -> Choice:
        from grading import example_args

        bare = q.get("category") == "bare_name"
        return Choice(q["expected_tool"], example_args(q.get("expected_args"), self.subs, bare_repo=bare))

    async def run_agent(self, q: dict[str, Any], call_tool: ToolCaller, max_steps: int = 8) -> AgentResult:
        raise SystemExit("The oracle backend only supports --mode select.")

    async def complete(self, prompt: str) -> str:
        raise SystemExit("The oracle backend cannot write descriptions; use ollama or anthropic.")
