"""Route scanner grader calls to the configured LLM backend."""

from __future__ import annotations

from typing import Any

from .. import config
from . import gemini_grader, openai_grader


async def grade_alert(state: dict[str, Any]) -> tuple[dict[str, Any] | None, int, float]:
    if config.grader_provider() == "openai":
        return await openai_grader.grade_alert(state)
    return await gemini_grader.grade_alert(state)
