"""SSE 스트리밍 응답 빌더 유틸리티"""

import asyncio
import json
from typing import Any, AsyncGenerator, Dict

from app.core.constants import STREAM_CHUNK_DELAY, STREAM_FINISH_MARKER


def _build_streaming_chunk(chunk_id: str, created: int, model: str, content: str) -> str:
    """단일 SSE 청크 문자열 생성."""
    chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"content": content}, "finish_reason": None}],
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


async def _build_streaming_response(
    content: str,
    response_template: Dict[str, Any],
) -> AsyncGenerator[str, None]:
    """응답 문자열을 문자 단위 SSE 청크로 yield."""
    chunk_id = response_template["id"]
    created = response_template["created"]
    model = response_template["model"]
    for char in content:
        yield _build_streaming_chunk(chunk_id, created, model, char)
        await asyncio.sleep(STREAM_CHUNK_DELAY)
    yield f"data: {STREAM_FINISH_MARKER}\n\n"


async def _stream_delta_content(content: str) -> AsyncGenerator[str, None]:
    """delta content 형식으로 문자 단위 SSE 청크 yield."""
    for char in content:
        chunk_data = {
            "choices": [{"index": 0, "delta": {"content": char}, "finish_reason": None}]
        }
        yield f"data: {json.dumps(chunk_data, ensure_ascii=False)}\n\n"
        await asyncio.sleep(STREAM_CHUNK_DELAY)
