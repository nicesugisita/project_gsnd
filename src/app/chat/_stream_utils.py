"""SSE 스트리밍 응답 빌더 유틸리티"""

import asyncio
import json
from typing import Any, AsyncGenerator, Dict

from app.core.constants import STREAM_CHUNK_DELAY, STREAM_FINISH_MARKER

# SSE(text/event-stream) 응답 공용 헤더.
# - X-Accel-Buffering: nginx/리버스 프록시의 응답 버퍼링 비활성화
# - Cache-Control: 캐시·압축 트랜스폼 차단(중간 게이트웨이가 chunk를 모아서 보내는 것 방지)
# - Connection: keep-alive 명시
SSE_RESPONSE_HEADERS: Dict[str, str] = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


# status_queue drain 종료 표시용 내부 센티넬
_STATUS_DRAIN_SENTINEL: Any = object()


async def drain_status_until_done(
    rag_task: asyncio.Task,
    status_queue: asyncio.Queue,
) -> AsyncGenerator[str, None]:
    """rag_task가 완료될 때까지 status_queue 메시지를 폴링 없이 즉시 yield.

    - status_queue에 들어오는 즉시 yield (대기 시간 0)
    - rag_task가 완료되는 즉시 다음 단계(LLM stream 소비)로 넘어가도록
      별도 watcher가 센티넬을 큐에 넣어 드레인 종료를 알림
    - rag_task 자체의 결과/예외는 호출 측에서 await로 처리해야 함

    Args:
        rag_task: 백엔드 RAG 처리 비동기 작업
        status_queue: rag_task가 진행 상태 메시지를 enqueue하는 큐
    """

    async def _watch() -> None:
        # rag_task의 결과/예외는 호출측 await rag_task에서 처리하므로 여기선 swallow.
        # KeyboardInterrupt/SystemExit는 일반 처리하지 않고 그대로 전파.
        try:
            await asyncio.shield(rag_task)
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            try:
                await status_queue.put(_STATUS_DRAIN_SENTINEL)
            except Exception:  # noqa: BLE001
                pass

    watcher = asyncio.create_task(_watch())
    try:
        while True:
            msg = await status_queue.get()
            if msg is _STATUS_DRAIN_SENTINEL:
                break
            yield msg
        # rag_task 완료 후 큐에 남아 있을 수 있는 잔여 메시지 즉시 배출
        while True:
            try:
                msg = status_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if msg is _STATUS_DRAIN_SENTINEL:
                continue
            yield msg
    finally:
        if not watcher.done():
            watcher.cancel()
            try:
                await watcher
            except (Exception, asyncio.CancelledError):
                pass


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
