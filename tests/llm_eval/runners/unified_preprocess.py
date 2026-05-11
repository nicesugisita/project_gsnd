"""Adapter: unified_preprocess."""
from __future__ import annotations

import app.chat.infra.rag  # noqa: F401  — pre-import to break circular load
from app.chat.preprocessing import unified_preprocess

from . import register


async def _run(inputs: dict) -> dict:
    user_query = inputs.get("user_query") or ""
    if not user_query:
        raise ValueError("unified_preprocess runner requires 'user_query'")

    result = await unified_preprocess(
        user_query=user_query,
        messages=inputs.get("messages") or [],
        use_rag=bool(inputs.get("use_rag", True)),
    )
    return dict(result)


register("unified_preprocess", _run)
