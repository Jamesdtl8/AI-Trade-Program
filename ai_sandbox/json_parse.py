"""Shared JSON extraction helpers (no LLM dependency)."""

from __future__ import annotations

import json
from typing import Any


def strip_json_fences(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("```"):
        return text
    lines = text.split("\n")
    if lines and lines[0].lstrip().startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    text = "\n".join(lines).strip()
    if text.lower().startswith("json"):
        text = text[4:].strip()
    return text


def extract_balanced_json_object(raw: str) -> str | None:
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return raw[start : i + 1]
    return None


def parse_decision_json(text: str) -> dict[str, Any] | None:
    raw = (text or "").strip()
    if not raw:
        return None
    bracket = extract_balanced_json_object(raw)
    candidates = [strip_json_fences(raw), raw]
    if bracket:
        candidates.extend([bracket, strip_json_fences(bracket)])
    seen: set[str] = set()
    for cand in candidates:
        c = cand.strip()
        if not c or c in seen:
            continue
        seen.add(c)
        try:
            out = json.loads(c)
        except json.JSONDecodeError:
            continue
        if not isinstance(out, dict):
            continue
        if out.get("decision") is not None or out.get("action") or out.get("grade"):
            return out
    return None
