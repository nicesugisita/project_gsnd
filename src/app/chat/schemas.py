"""Chat 도메인 스키마 — core/models의 ChatRequest 등을 re-export."""

from app.shared.schemas import (
    ChatRequest,
    RecommendStartRequest,
)

__all__ = ["ChatRequest", "RecommendStartRequest"]
