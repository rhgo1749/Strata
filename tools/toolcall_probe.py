#!/usr/bin/env python3
"""Small OpenAI-compatible tool-call probe for a running Strata server.

This is intentionally stdlib-only so it can be used on a host checkout without installing a
benchmark client.  It checks both sides of agent reliability: choosing/calling the right tool
with JSON arguments, and *not* calling a tool when the request says not to.
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable


WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Get current weather for a city.",
        "parameters": {
            "type": "object",
            "properties": {
                "city": {"type": "string"},
                "unit": {"type": "string", "enum": ["celsius", "fahrenheit"]},
            },
            "required": ["city", "unit"],
        },
    },
}
CALC = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": "Apply one arithmetic operation to two numbers.",
        "parameters": {
            "type": "object",
            "properties": {
                "a": {"type": "number"},
                "b": {"type": "number"},
                "op": {"type": "string", "enum": ["add", "mul"]},
            },
            "required": ["a", "b", "op"],
        },
    },
}
NOTE = {
    "type": "function",
    "function": {
        "name": "save_note",
        "description": "Save a structured note.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "priority": {"type": "integer"},
                "pinned": {"type": "boolean"},
                "meta": {"type": "object"},
            },
            "required": ["title", "tags", "priority", "pinned", "meta"],
        },
    },
}
TOOLS = [WEATHER, CALC, NOTE]


@dataclass(frozen=True)
class Case:
    name: str
    prompt: str
    tool_choice: Any
    check: Callable[[dict], tuple[bool, str]]


def _tool(msg: dict) -> tuple[str | None, dict | None, str]:
    calls = msg.get("tool_calls") or []
    if not calls:
        return None, None, "no tool call"
    if len(calls) != 1:
        return None, None, f"expected one tool call, got {len(calls)}"
    fn = calls[0].get("function") or {}
    raw = fn.get("arguments") or "{}"
    try:
        args = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as e:
        return fn.get("name"), None, f"invalid arguments JSON: {e}: {raw!r}"
    return fn.get("name"), args, ""


def weather_check(msg: dict) -> tuple[bool, str]:
    name, args, err = _tool(msg)
    ok = name == "get_weather" and args == {"city": "Busan", "unit": "celsius"}
    return ok, err or f"got {name} {args}"


def calc_check(msg: dict) -> tuple[bool, str]:
    name, args, err = _tool(msg)
    ok = name == "calculate" and args == {"a": 13, "b": 7, "op": "mul"}
    return ok, err or f"got {name} {args}"


def note_check(msg: dict) -> tuple[bool, str]:
    name, args, err = _tool(msg)
    want = {"title": "alpha", "tags": ["x", "y"], "priority": 2, "pinned": True,
            "meta": {"source": "hermes", "retry": 1}}
    ok = name == "save_note" and args == want
    return ok, err or f"got {name} {args}"


def none_check(msg: dict) -> tuple[bool, str]:
    calls = msg.get("tool_calls") or []
    content = (msg.get("content") or "").strip().upper()
    ok = not calls and content == "PONG"
    return ok, f"calls={len(calls)} content={msg.get('content')!r}"


CASES = [
    Case("weather", "Use the weather tool for Busan in celsius. Do not guess.", "required", weather_check),
    Case("calculate", "Use the calculator tool to multiply 13 by 7.", "required", calc_check),
    Case("structured", "Save exactly: title alpha; tags x,y; priority 2; pinned true; meta source=hermes retry=1.",
         {"type": "function", "function": {"name": "save_note"}}, note_check),
    Case("no-tool", "Reply with exactly PONG. Do not call any tool.", "none", none_check),
]


def post(url: str, body: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:18087/v1/chat/completions")
    ap.add_argument("--temperatures", default="0,0.2,0.4,0.7,1.0")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--max-tokens", type=int, default=320)
    a = ap.parse_args()

    temps = [float(x.strip()) for x in a.temperatures.split(",") if x.strip()]
    total_fail = 0
    for temp in temps:
        passed = 0
        durations = []
        print(f"\n== temperature {temp:g} ==")
        for run in range(1, a.runs + 1):
            for case in CASES:
                body = {
                    "model": "strata-probe",
                    "messages": [{"role": "user", "content": case.prompt}],
                    "tools": TOOLS,
                    "tool_choice": case.tool_choice,
                    "temperature": temp,
                    "max_tokens": a.max_tokens,
                }
                t0 = time.monotonic()
                try:
                    result = post(a.url, body, a.timeout)
                    dt = time.monotonic() - t0
                    durations.append(dt)
                    choice = result["choices"][0]
                    ok, detail = case.check(choice["message"])
                    if case.name != "no-tool":
                        ok = ok and choice.get("finish_reason") == "tool_calls"
                    else:
                        ok = ok and choice.get("finish_reason") == "stop"
                    passed += int(ok)
                    print(f"{case.name:10s} run={run} {'PASS' if ok else 'FAIL'} {dt:5.2f}s "
                          f"finish={choice.get('finish_reason')} {detail if not ok else ''}")
                except Exception as e:  # probe must report transport/server/parser failures instead of stopping
                    total_fail += 1
                    print(f"{case.name:10s} run={run} ERROR {type(e).__name__}: {e}")
        expected = a.runs * len(CASES)
        total_fail += expected - passed
        mean = sum(durations) / len(durations) if durations else 0.0
        print(f"SUMMARY temperature={temp:g} pass={passed}/{expected} mean_s={mean:.2f}")
    return 1 if total_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
