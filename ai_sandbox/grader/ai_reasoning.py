"""Plain-English explanations for GPT grader decisions (UI / audit)."""

from __future__ import annotations

import re
from typing import Any

from ..human_labels import humanize

_GATE_NAMES = {
    "gate_1": "Float / market cap",
    "gate_2": "News catalyst",
    "gate_3": "Squeeze structure",
    "gate_4": "Price / RV velocity",
}

_OVERRIDE_REASONS = {
    "known_runner_continuation_override": (
        "System upgrade: Known Runner still running with heavy volume — "
        "trade allowed even without a news headline."
    ),
    "momentum_override_4plus": (
        "System upgrade: four or more rising momentum alerts with RV above 100x."
    ),
    "rv_momentum_tolerance": (
        "System adjustment: modest RV dip but price and momentum still intact."
    ),
    "grade_action_normalized": (
        "System corrected conflicting grade/action fields from the model."
    ),
    "news_momentum_override": (
        "System upgrade — tight float, news catalyst, and RV at least 90x; "
        "squeeze flags not required for TRADE."
    ),
    "reentry_guard": (
        "Re-entry blocked — already traded this ticker today; "
        "setup did not meet stricter re-validation rules."
    ),
}

_VALID_GRADE_ACTION = {
    ("STRONG", "TRADE"),
    ("WATCH", "MONITOR"),
    ("SKIP", "PASS"),
}

_OUTCOME_OPENERS = {
    ("STRONG", "TRADE"): "Taking the trade.",
    ("WATCH", "MONITOR"): "Watching only — not entering yet.",
    ("SKIP", "PASS"): "Passing on this setup.",
}

_TECHNICAL_MARKERS = (
    "pass_strong",
    "gate 1",
    "gate 2",
    "gate 3",
    "gate 4",
    "graded skip",
    "graded watch",
    "graded strong",
    "→ monitor",
    "→ pass",
    "→ trade",
)


_SUMMARY_MAX_SENTENCES = 2
_SUMMARY_MAX_CHARS = 240


def clamp_summary_text(text: str, *, max_sentences: int = _SUMMARY_MAX_SENTENCES, max_chars: int = _SUMMARY_MAX_CHARS) -> str:
    """Keep dashboard summaries short — about two lines."""
    raw = " ".join(str(text or "").split()).strip()
    if not raw:
        return raw
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", raw) if p.strip()]
    if not parts:
        parts = [raw]
    clipped = " ".join(parts[:max_sentences]).strip()
    if len(clipped) > max_chars:
        clipped = clipped[: max_chars - 1].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return clipped


def align_summary_to_outcome(output: dict[str, Any], grade: str, action: str) -> None:
    """Rewrite summary opener to match postprocess-adjusted grade/action."""
    g, a, _ = normalize_grade_action(grade, action)
    headline = _OUTCOME_OPENERS.get((g, a), "")
    if not headline:
        return
    summary = str(output.get("summary") or "").strip()
    lower = summary.lower()
    for opener in _OUTCOME_OPENERS.values():
        prefix = opener.lower().rstrip(".")
        if lower.startswith(prefix):
            summary = summary[len(opener) :].lstrip(" .,-")
            lower = summary.lower()
            break
    for phrase in (
        "taking the trade",
        "watching only — not entering yet",
        "watching this setup",
        "passing on this setup",
    ):
        if lower.startswith(phrase):
            dot = summary.find(".")
            summary = summary[dot + 1 :].lstrip() if dot >= 0 else ""
            lower = summary.lower()
            break
    body = summary.strip()
    output["summary"] = clamp_summary_text(f"{headline} {body}".strip() if body else headline)


def normalize_grade_action(
    grade: str,
    action: str,
    *,
    gates: dict[str, Any] | None = None,
) -> tuple[str, str, str | None]:
    """Force canonical grade/action pairs from the grader spec."""
    g = str(grade or "SKIP").upper()
    a = str(action or "PASS").upper()
    if (g, a) in _VALID_GRADE_ACTION:
        return g, a, None

    g4 = str((gates or {}).get("gate_4") or "").upper()
    note = f"The model returned conflicting grade {g} with action {a}"
    if g == "SKIP" or g4 == "FAIL":
        return "SKIP", "PASS", f"{note}; treated as PASS (skip)"
    if a == "TRADE" or g == "STRONG":
        return "STRONG", "TRADE", f"{note}; treated as TRADE"
    if a == "MONITOR" or g == "WATCH":
        return "WATCH", "MONITOR", f"{note}; treated as WATCH"
    return "SKIP", "PASS", f"{note}; treated as PASS (skip)"


def outcome_headline(grade: str, action: str) -> str:
    g, a, _ = normalize_grade_action(grade, action)
    return _OUTCOME_OPENERS.get((g, a), f"{g} / {a}")


def _looks_technical(text: str) -> bool:
    t = text.lower()
    return any(marker in t for marker in _TECHNICAL_MARKERS)


def _gate_strengths_weaknesses(gates: dict[str, Any]) -> tuple[list[str], list[str]]:
    strengths: list[str] = []
    weaknesses: list[str] = []

    g1 = str(gates.get("gate_1") or "").upper()
    if g1.startswith("PASS"):
        if g1 == "PASS_STRONG":
            strengths.append("very tight float and market cap")
        else:
            strengths.append("float and market cap are acceptable for momentum")
    elif g1 == "PARTIAL":
        weaknesses.append("float or market cap is on the larger side")
    elif g1 == "FAIL":
        weaknesses.append("float or market cap is too large")

    g2 = str(gates.get("gate_2") or "").upper()
    if g2.startswith("PASS"):
        strengths.append("a clear news catalyst is present")
    elif g2 == "PARTIAL":
        weaknesses.append("news is weak or indirect — no strong headline catalyst")
    elif g2 == "FAIL":
        weaknesses.append("no named news catalyst in the alert history")

    g3 = str(gates.get("gate_3") or "").upper()
    if g3.startswith("PASS"):
        if g3 == "PASS_STRONG":
            strengths.append("multiple squeeze flags (0 Borrow, Reg SHO, etc.)")
        else:
            strengths.append("at least one squeeze / structural flag")
    elif g3 == "PARTIAL":
        strengths.append("Known Runner history (structure only partial)")
    elif g3 == "FAIL":
        weaknesses.append("no squeeze or structural support")

    g4 = str(gates.get("gate_4") or "").upper()
    if g4.startswith("PASS"):
        strengths.append("price and relative volume are still accelerating")
    elif g4 == "PARTIAL":
        weaknesses.append("RV dipped modestly but price is still rising")
    elif g4 == "FAIL":
        weaknesses.append("price or volume momentum has fallen off")

    return strengths, weaknesses


def build_narrative_summary(output: dict[str, Any]) -> str:
    """Fallback plain-English summary when GPT does not supply one."""
    ctx = output.get("context") if isinstance(output.get("context"), dict) else {}
    gates = ctx.get("gates") if isinstance(ctx.get("gates"), dict) else {}
    grade, action, mismatch = normalize_grade_action(
        str(output.get("grade") or "SKIP"),
        str(output.get("action") or "PASS"),
        gates=gates,
    )
    flags = list(output.get("risk_flags") or ctx.get("risk_flags") or [])
    catalyst = str(ctx.get("catalyst") or "").strip()
    strengths, weaknesses = _gate_strengths_weaknesses(gates)

    sentences: list[str] = [_OUTCOME_OPENERS.get((grade, action), "Decision recorded.")]

    if strengths:
        sentences.append("Working in its favour: " + "; ".join(strengths[:3]) + ".")
    if weaknesses:
        sentences.append("Working against it: " + "; ".join(weaknesses[:3]) + ".")

    if catalyst and catalyst.lower() not in ("same", "none", "n/a", ""):
        sentences.append(f"News noted: {catalyst[:220]}.")

    if flags:
        plain_flags = [str(f) for f in flags[:4] if str(f).strip()]
        if plain_flags:
            sentences.append("Extra risks: " + "; ".join(plain_flags) + ".")

    if action == "TRADE" and grade == "STRONG":
        entry = output.get("entry_price")
        target = output.get("target_price")
        if entry and target:
            sentences.append(f"Suggested entry around ${entry}, target near ${target}.")

    if mismatch:
        sentences.append(mismatch.capitalize() + ".")

    history = ctx.get("grade_change_history") if isinstance(ctx.get("grade_change_history"), list) else []
    for change in history:
        reason = change.get("reason")
        if not reason or reason == "grade_action_normalized":
            continue
        msg = _OVERRIDE_REASONS.get(str(reason), humanize(str(reason)))
        sentences.append(msg)

    return clamp_summary_text(" ".join(sentences))


def finalize_reasoning(output: dict[str, Any] | None) -> str:
    """Prefer GPT-written summary; otherwise build a readable narrative."""
    if not output:
        return "No AI decision recorded."

    ctx = output.get("context") if isinstance(output.get("context"), dict) else {}
    gates = ctx.get("gates") if isinstance(ctx.get("gates"), dict) else {}
    grade, action, mismatch = normalize_grade_action(
        str(output.get("grade") or "SKIP"),
        str(output.get("action") or "PASS"),
        gates=gates,
    )
    history = ctx.get("grade_change_history") if isinstance(ctx.get("grade_change_history"), list) else []

    gpt_summary = str(
        output.get("summary") or output.get("reasoning") or output.get("reason") or ""
    ).strip()

    if gpt_summary and not _looks_technical(gpt_summary):
        text = gpt_summary
    else:
        text = build_narrative_summary(output)

    extras: list[str] = []
    if mismatch and gpt_summary and not _looks_technical(gpt_summary):
        extras.append(mismatch.capitalize() + ".")
    for change in history:
        if not isinstance(change, dict):
            continue
        reason = change.get("reason")
        if not reason or reason == "grade_action_normalized":
            continue
        if reason in _OVERRIDE_REASONS:
            msg = _OVERRIDE_REASONS[reason]
            if msg not in text:
                extras.append(msg)
        frm = change.get("from")
        to = change.get("to")
        if frm and to and frm != to and reason not in _OVERRIDE_REASONS:
            msg = f"Adjusted from {frm} to {to}: {humanize(str(reason))}."
            if msg not in text:
                extras.append(msg)

    if extras:
        text = text.rstrip(".") + ". " + " ".join(extras)

    # Ensure opener matches final normalized outcome when we built from gates only
    if not gpt_summary or _looks_technical(gpt_summary):
        return text

    expected = _OUTCOME_OPENERS.get((grade, action), "")
    if expected and expected.split(".")[0].lower() not in text.lower()[:80]:
        text = expected + " " + text

    return clamp_summary_text(text.strip())


def explain_ai_decision(output: dict[str, Any] | None) -> str:
    """Alias used by DB/API — always returns dashboard-friendly prose."""
    return finalize_reasoning(output)
