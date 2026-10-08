"""Argument-level grading for the tool-selection eval.

A question's optional `expected_args` maps parameter names to one of:
  "value"              exact match (strings compare case-insensitively)
  null                 the argument must be absent (e.g. no `repo` for "across all my repos")
  {"contains": "s"}    a string argument containing s, case-insensitively
  {"any": [a, b]}      any of these values; null in the list allows "absent"
  {"range": [lo, hi]}  a number between lo and hi inclusive

Placeholders like "{repo}" are filled from the same substitutions as the question.
`repo` is compared by meaning: "owner/name", a bare "name", or a GitHub URL all match.
Parameters not listed are not graded. When an expected value equals the tool's default,
leaving the argument out counts as a match.
"""

from __future__ import annotations

import re
from typing import Any

URL_PREFIX = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/", re.IGNORECASE)


def fill(value: Any, subs: dict[str, str]) -> Any:
    if isinstance(value, str):
        return value.format(**subs)
    if isinstance(value, list):
        return [fill(v, subs) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, subs) for k, v in value.items()}
    return value


def _norm_repo(value: str) -> str:
    value = URL_PREFIX.sub("", value.strip()).rstrip("/").lower()
    return value[:-4] if value.endswith(".git") else value


def _repo_matches(expected: str, actual: Any) -> bool:
    if not isinstance(actual, str):
        return False
    exp, act = _norm_repo(expected), _norm_repo(actual)
    return act == exp or ("/" not in act and act == exp.split("/", 1)[-1])


def _value_matches(name: str, expected: Any, actual: Any) -> bool:
    if name == "repo" and isinstance(expected, str):
        return _repo_matches(expected, actual)
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip().lower() == actual.strip().lower()
    if isinstance(expected, (int, float)) and isinstance(actual, (int, float, str)):
        try:
            return float(actual) == float(expected)
        except ValueError:
            return False
    return expected == actual


def check_args(
    expected: dict[str, Any] | None,
    actual: dict[str, Any],
    defaults: dict[str, Any],
    subs: dict[str, str],
) -> list[str]:
    """Return human-readable mismatches; an empty list means the arguments are right."""
    if not expected:
        return []
    problems = []
    for name, spec in fill(expected, subs).items():
        present = actual.get(name) not in (None, "")
        value = actual.get(name) if present else defaults.get(name)
        if spec is None:
            if present:
                problems.append(f"{name}: expected absent, got {actual[name]!r}")
            continue
        if isinstance(spec, dict) and "contains" in spec:
            ok = isinstance(value, str) and spec["contains"].lower() in value.lower()
        elif isinstance(spec, dict) and "range" in spec:
            lo, hi = spec["range"]
            try:
                ok = value is not None and lo <= float(value) <= hi
            except (TypeError, ValueError):
                ok = False
        elif isinstance(spec, dict) and "any" in spec:
            ok = any((not present and value is None) if opt is None else _value_matches(name, opt, value)
                     for opt in spec["any"])
        else:
            ok = value is not None and _value_matches(name, spec, value)
        if not ok:
            problems.append(f"{name}: expected {spec!r}, got {actual.get(name)!r}")
    return problems


def example_args(expected: dict[str, Any] | None, subs: dict[str, str], *, bare_repo: bool = False) -> dict[str, Any]:
    """A concrete argument set that satisfies `expected` (used by the oracle backend)."""
    out: dict[str, Any] = {}
    for name, spec in fill(expected or {}, subs).items():
        if spec is None:
            continue
        if isinstance(spec, dict):
            if "contains" in spec:
                spec = spec["contains"]
            elif "range" in spec:
                spec = spec["range"][0]
            elif "any" in spec:
                options = [o for o in spec["any"] if o is not None]
                if not options:
                    continue
                spec = options[0]
        if name == "repo" and bare_repo and isinstance(spec, str):
            spec = spec.split("/", 1)[-1]
        out[name] = spec
    return out


def schema_defaults(input_schema: dict[str, Any]) -> dict[str, Any]:
    return {k: v["default"] for k, v in input_schema.get("properties", {}).items() if "default" in v}
