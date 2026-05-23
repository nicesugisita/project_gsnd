"""응답 트레이스 sink — 사용자 질문 / 참조문서 / 최종 프롬프트 / 응답 본문을 JSONL+xlsx로 저장.

토글: Config.RESPONSE_TRACE_ENABLED
출력 디렉토리: Config.RESPONSE_TRACE_DIR
파일:
- response_trace.jsonl — append-only, raw 전체 보관 (디버그·재처리용)
- response_trace.xlsx  — append, 셀 30,000자 컷, 사람이 보기 좋은 요약

호출 패턴:
- 비스트리밍: `record_response_trace(...)` 직접 호출
- 스트리밍:   `wrap_stream_with_trace(generator, trace_meta)` 로 generator를 감싸면
              chunk 누적 → 종료 시 자동 dump

xlsx 쓰기는 asyncio.Lock 으로 직렬화. fire-and-forget(create_task) 방식이라
응답 latency 에는 영향 없음. Lock 으로 동시 write corruption 차단.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import os
import re
from typing import Any, AsyncGenerator, Dict, List, Optional
import uuid

logger = logging.getLogger(__name__)


# xlsx 셀 한도 (32,767) 보다 약간 작게 잘라 안전 마진 확보.
_XLSX_CELL_MAX_CHARS = 30_000
# JSONL 한 줄 raw 보관용. 너무 큰 docs 는 잘라 디스크 폭주 방지.
_JSONL_DOC_MAX_CHARS = 8_000

# SSE 라인에서 JSON 만 잘라내는 정규식. "data: {...}" 형태.
_SSE_DATA_PREFIX = re.compile(r"^data:\s*")

_XLSX_LOCK: Optional[asyncio.Lock] = None


def _get_xlsx_lock() -> asyncio.Lock:
    """이벤트 루프 바운드 Lock 지연 초기화."""
    global _XLSX_LOCK
    if _XLSX_LOCK is None:
        _XLSX_LOCK = asyncio.Lock()
    return _XLSX_LOCK


# ============================================================
# Config 접근
# ============================================================


def _is_enabled() -> bool:
    try:
        from app.core.config import Config
        return bool(getattr(Config, "RESPONSE_TRACE_ENABLED", False))
    except Exception:
        return False


def _trace_dir() -> str:
    from app.core.config import Config
    return getattr(Config, "RESPONSE_TRACE_DIR", "log/response_trace")


def _ensure_dir(path: str) -> None:
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        logger.warning("[trace_sink] 디렉토리 생성 실패 (%s): %s", path, e)


# ============================================================
# 직렬화 / 요약 헬퍼
# ============================================================


_DOC_KEY_CANDIDATES = (
    "NAME", "name", "title", "BUSINESS_NAME",
    "CHUNK_PATH", "chunk_path",
    "content", "CONTENT", "chunk",
)


def _summarize_doc(doc: Dict[str, Any], idx: int) -> Dict[str, Any]:
    """top_docs 한 건을 사람이 읽기 좋은 요약 dict 로 추림.

    raw 원본은 너무 크므로 핵심 필드 (이름·경로·본문 앞부분) 만 추출.
    """
    if not isinstance(doc, dict):
        return {"idx": idx, "value": str(doc)[:_JSONL_DOC_MAX_CHARS]}
    out: Dict[str, Any] = {"idx": idx}
    for key in _DOC_KEY_CANDIDATES:
        if key in doc and doc[key] is not None:
            v = str(doc[key])
            if len(v) > _JSONL_DOC_MAX_CHARS:
                v = v[:_JSONL_DOC_MAX_CHARS] + f"...[truncated {len(v) - _JSONL_DOC_MAX_CHARS} chars]"
            out[key] = v
    return out


def _summarize_top_docs(top_docs: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    if not top_docs:
        return []
    return [_summarize_doc(d, i) for i, d in enumerate(top_docs, 1)]


def _truncate_for_cell(text: Any) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= _XLSX_CELL_MAX_CHARS:
        return s
    return s[:_XLSX_CELL_MAX_CHARS] + f"...[truncated {len(s) - _XLSX_CELL_MAX_CHARS} chars]"


# ============================================================
# SSE chunk 파싱
# ============================================================


def extract_text_from_sse_chunks(chunks: List[str]) -> str:
    """SSE 형식 chunk 리스트("data: {json}\\n\\n" 등)에서 응답 텍스트 누적.

    [DONE] 종료 마커는 무시. JSON 파싱 실패 라인은 skip.
    OpenAI 호환 schema: choices[0].delta.content 가 chunk 텍스트.
    비-OpenAI 형식(content 가 메시지 본문에 바로 있는 경우)도 보조 처리.
    """
    parts: List[str] = []
    for chunk in chunks:
        if not chunk:
            continue
        for line in str(chunk).splitlines():
            line = line.strip()
            if not line:
                continue
            m = _SSE_DATA_PREFIX.match(line)
            if m:
                payload = line[m.end():].strip()
            else:
                payload = line
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except (json.JSONDecodeError, ValueError):
                # 파싱 실패한 chunk 는 raw 그대로 (디버그 용)
                parts.append(payload)
                continue
            text = _extract_text_from_chunk_object(obj)
            if text:
                parts.append(text)
    return "".join(parts)


def _extract_text_from_chunk_object(obj: Any) -> str:
    """OpenAI 호환 chunk dict 에서 텍스트 추출. 미지 schema 는 ''."""
    if not isinstance(obj, dict):
        return ""
    choices = obj.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            # 스트리밍: delta.content
            delta = first.get("delta")
            if isinstance(delta, dict):
                c = delta.get("content")
                if isinstance(c, str):
                    return c
            # 비스트리밍 호환: message.content
            msg = first.get("message")
            if isinstance(msg, dict):
                c = msg.get("content")
                if isinstance(c, str):
                    return c
            # 일부 서버는 choices[0].text
            t = first.get("text")
            if isinstance(t, str):
                return t
    # 일부 서버: 최상위 content
    c = obj.get("content")
    if isinstance(c, str):
        return c
    return ""


# ============================================================
# JSONL append
# ============================================================


def _write_jsonl_record(record: Dict[str, Any]) -> None:
    """JSONL append. 동기 IO — 매우 빠름 (수 KB)."""
    try:
        directory = _trace_dir()
        _ensure_dir(directory)
        path = os.path.join(directory, "response_trace.jsonl")
        line = json.dumps(record, ensure_ascii=False, default=str)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        logger.warning("[trace_sink] JSONL append 실패: %s", e)


# ============================================================
# xlsx append
# ============================================================


_XLSX_HEADERS = [
    "trace_id", "ts", "intent", "is_stream",
    "user_question", "system_prompt", "user_message",
    "top_docs_count", "top_docs_summary",
    "response_text",
    "extras",
]


def _xlsx_path() -> str:
    return os.path.join(_trace_dir(), "response_trace.xlsx")


def _write_xlsx_record_sync(record: Dict[str, Any]) -> None:
    """xlsx 한 행 append. 매 호출마다 load → append → save (openpyxl).

    동시 호출은 호출자 측 asyncio.Lock 으로 직렬화한다고 가정.
    """
    try:
        from openpyxl import Workbook, load_workbook
    except ImportError as e:
        logger.warning("[trace_sink] openpyxl import 실패: %s — xlsx skip", e)
        return

    path = _xlsx_path()
    try:
        if os.path.exists(path):
            wb = load_workbook(path)
            ws = wb.active
        else:
            wb = Workbook()
            ws = wb.active
            ws.title = "response_trace"
            ws.append(_XLSX_HEADERS)

        row = [
            record.get("trace_id", ""),
            record.get("ts", ""),
            record.get("intent", ""),
            "stream" if record.get("is_stream") else "sync",
            _truncate_for_cell(record.get("user_question")),
            _truncate_for_cell(record.get("system_prompt")),
            _truncate_for_cell(record.get("user_message")),
            len(record.get("top_docs") or []),
            _truncate_for_cell(
                json.dumps(record.get("top_docs") or [], ensure_ascii=False, default=str)
            ),
            _truncate_for_cell(record.get("response_text")),
            _truncate_for_cell(
                json.dumps(record.get("extras") or {}, ensure_ascii=False, default=str)
            ),
        ]
        ws.append(row)
        wb.save(path)
    except Exception as e:  # noqa: BLE001 — 트레이스가 응답 흐름을 깨면 안 됨
        logger.warning("[trace_sink] xlsx append 실패: %s", e)


# ============================================================
# Public API
# ============================================================


def build_trace_record(
    *,
    user_question: str,
    top_docs: Optional[List[Dict[str, Any]]],
    system_prompt: str,
    user_message: str,
    intent: str,
    response_text: str = "",
    is_stream: bool = False,
    extras: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """JSONL/xlsx 양쪽에 쓸 trace 레코드 1건 생성."""
    return {
        "trace_id": uuid.uuid4().hex[:12],
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "intent": intent or "",
        "is_stream": bool(is_stream),
        "user_question": user_question or "",
        "system_prompt": system_prompt or "",
        "user_message": user_message or "",
        "top_docs": _summarize_top_docs(top_docs),
        "response_text": response_text or "",
        "extras": dict(extras or {}),
    }


async def _persist_record(record: Dict[str, Any]) -> None:
    """JSONL 동기 write + xlsx 는 Lock 직렬화."""
    _write_jsonl_record(record)
    lock = _get_xlsx_lock()
    async with lock:
        # openpyxl 는 동기 라이브러리. 짧은 IO 라 그대로 호출.
        # (필요하면 to_thread 로 이전 가능 — 현재 부하 수준에선 직접 호출이 단순.)
        _write_xlsx_record_sync(record)


def record_response_trace(
    *,
    user_question: str,
    top_docs: Optional[List[Dict[str, Any]]],
    system_prompt: str,
    user_message: str,
    intent: str,
    response_text: str,
    extras: Optional[Dict[str, Any]] = None,
) -> None:
    """비스트리밍 응답용: 응답 본문까지 받은 직후 호출. fire-and-forget."""
    if not _is_enabled():
        return
    record = build_trace_record(
        user_question=user_question, top_docs=top_docs,
        system_prompt=system_prompt, user_message=user_message,
        intent=intent, response_text=response_text,
        is_stream=False, extras=extras,
    )
    try:
        asyncio.get_running_loop().create_task(_persist_record(record))
    except RuntimeError:
        # 이벤트 루프 밖에서 호출된 경우 (테스트 등) — 동기로 폴백.
        _write_jsonl_record(record)
        _write_xlsx_record_sync(record)


async def wrap_stream_with_trace(
    generator: AsyncGenerator[str, None],
    *,
    user_question: str,
    top_docs: Optional[List[Dict[str, Any]]],
    system_prompt: str,
    user_message: str,
    intent: str,
    extras: Optional[Dict[str, Any]] = None,
) -> AsyncGenerator[str, None]:
    """스트리밍 응답용: generator 를 감싸 chunk 누적 후 종료 시 dump.

    토글이 꺼져 있으면 wrap 비용 없이 그대로 pass-through.
    """
    if not _is_enabled():
        async for chunk in generator:
            yield chunk
        return

    chunks: List[str] = []
    try:
        async for chunk in generator:
            chunks.append(chunk)
            yield chunk
    finally:
        try:
            response_text = extract_text_from_sse_chunks(chunks)
        except Exception as e:  # noqa: BLE001
            logger.warning("[trace_sink] SSE chunk 텍스트 추출 실패: %s", e)
            response_text = ""
        record = build_trace_record(
            user_question=user_question, top_docs=top_docs,
            system_prompt=system_prompt, user_message=user_message,
            intent=intent, response_text=response_text,
            is_stream=True, extras=extras,
        )
        try:
            asyncio.get_running_loop().create_task(_persist_record(record))
        except RuntimeError:
            _write_jsonl_record(record)
            _write_xlsx_record_sync(record)
