"""Adapter: classify_next_intent."""
from __future__ import annotations

# Importing app.chat.infra.rag first avoids a circular import via routing.py.
import app.chat.infra.rag  # noqa: F401
from app.chat.routing import classify_next_intent

from . import register


async def _run(inputs: dict) -> dict:
    messages = inputs.get("messages") or []
    user_query = inputs.get("user_query") or ""
    if not user_query:
        raise ValueError("next_intent runner requires 'user_query'")

    result = await classify_next_intent(
        messages=messages,
        current_query=user_query,
        prior_intent=inputs.get("prior_intent") or "",
        prior_service_names=inputs.get("prior_service_names") or [],
        is_clarification_question=bool(inputs.get("is_clarification_question", False)),
    )
    return dict(result)


register("next_intent", _run)
