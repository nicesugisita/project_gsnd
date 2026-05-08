"""대화 세션 관리: 락, 히스토리 병합, 유저 제한, 메시지 전처리, 히스토리 저장"""

import asyncio
import logging
import uuid
from typing import Dict, Optional

from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import Config

from app.shared.schemas import ChatRequest
from app.chat.service import clean_query_text, convert_korean_to_standard
from app.conversation.history import get_chat_history_service
from app.chat.user_session import get_user_session_service
from app.shared.utils import build_chat_response
from app.dependencies import (
    _resolve_chat_history_user_id,
    _save_chat_history,
    _update_user_message,
    chat_user_id_requires_nologin_canonical,
    ensure_conversation_exists,
)
from ._stream_utils import _build_streaming_response, SSE_RESPONSE_HEADERS

logger = logging.getLogger(__name__)

# per-conv_id 락: 동시 요청으로 인한 히스토리 병합 Race Condition 방지
_conv_locks: Dict[str, asyncio.Lock] = {}
_CONV_LOCKS_MAX = Config.CONV_LOCKS_MAX


def _get_conv_lock(conv_id: str) -> asyncio.Lock:
    """conv_id별 asyncio.Lock을 반환합니다. 락 수가 임계치 초과 시 오래된 것부터 정리합니다."""
    if conv_id not in _conv_locks:
        if len(_conv_locks) > _CONV_LOCKS_MAX:
            keys = list(_conv_locks.keys())
            for key in keys[: len(keys) // 2]:
                _conv_locks.pop(key, None)
        _conv_locks[conv_id] = asyncio.Lock()
    return _conv_locks[conv_id]


def _do_merge(chat_request: ChatRequest) -> None:
    """히스토리 병합 및 conv_id 초기화 실제 로직 (락 내부에서 실행)."""
    history_service = get_chat_history_service(Config)

    # conv_id 우선 고정 후 조회해야 비로그인도 로그인과 같이 행 단일성이 보장된다.
    if not chat_request.conv_id:
        chat_request.conv_id = str(uuid.uuid4())

    cid = str(chat_request.conv_id).strip()
    canon = cid
    guest = chat_user_id_requires_nologin_canonical(chat_request.user_id)

    # 비로그인: 세션은 conv_id만 있으면 됨 → 히스토리 행 항상 (user_id=cid, conv_id=cid) 한 줄
    if guest:
        chat_request.user_id = canon
        history_scope = canon
    else:
        uid = chat_request.user_id.strip() if isinstance(chat_request.user_id, str) else ""
        if uid.startswith("nologin"):
            chat_request.user_id = canon
            history_scope = canon
        elif uid:
            history_scope = uid
        else:
            history_scope = _resolve_chat_history_user_id(chat_request.user_id, cid)

    db_history = history_service.get_history(cid, history_scope) or []
    front_messages = chat_request.messages or []

    def msg_key(m):
        return (m.get("role", ""), m.get("content", ""))

    new_messages = front_messages
    db_len, front_len = len(db_history), len(front_messages)
    for overlap in range(min(db_len, front_len), 0, -1):
        if all(
            msg_key(db_history[db_len - overlap + i]) == msg_key(front_messages[i])
            for i in range(overlap)
        ):
            new_messages = front_messages[overlap:]
            logger.info(f"[MULTI TURN] 중복 {overlap}개 메시지 제거 후 신규 {len(new_messages)}개 병합")
            break

    chat_request.messages = db_history + new_messages
    logger.info(f"[MULTI TURN MERGED] {chat_request.messages}")

    persist_for_conv = _resolve_chat_history_user_id(chat_request.user_id, cid)
    ensure_conversation_exists(cid, persist_for_conv, chat_request.messages)


async def _merge_and_init_conversation(chat_request: ChatRequest) -> None:
    """
    DB 히스토리와 프론트 messages를 병합하고 conv_id를 초기화합니다.
    동일 conv_id의 동시 요청 Race Condition 방지를 위해 per-conv_id 락을 사용합니다.
    """
    conv_id = chat_request.conv_id or ""
    if conv_id:
        lock = _get_conv_lock(conv_id)
        async with lock:
            await asyncio.to_thread(_do_merge, chat_request)
    else:
        await asyncio.to_thread(_do_merge, chat_request)


def _check_user_limit(
    user_message: str,
    chat_request: ChatRequest,
    stream: bool,
) -> Optional[JSONResponse | StreamingResponse]:
    """
    동시 사용자 수 제한을 확인합니다.
    제한 초과 시 에러 Response를 반환하고, 허용 시 None을 반환합니다.
    """
    user_identifier = chat_request.user_id or chat_request.conv_id or ""
    if not user_identifier:
        return None

    user_session_service = get_user_session_service(Config)
    is_allowed, error_message = user_session_service.check_user_limit(user_identifier)
    if is_allowed:
        return None

    response_message = error_message or "현재 사용자 수가 많아 서버가 부하 상태입니다. 조금 후에 사용해 주세요."
    response = build_chat_response(
        response_message=response_message,
        user_message=user_message,
        model_name=Config.MODEL_NAME,
    )
    if stream:
        return StreamingResponse(
            _build_streaming_response(response["choices"][0]["message"]["content"], response),
            media_type="text/event-stream",
            headers=SSE_RESPONSE_HEADERS,
        )
    return JSONResponse(content=response, status_code=200)


async def _preprocess_message(user_message: str, chat_request: ChatRequest) -> str:
    """
    텍스트 클리닝과 한국어 표준어 변환을 수행합니다.
    user_message는 텍스트 클리닝만 반영하고, 표준어 변환은 messages에만 적용됩니다.
    """
    if Config.TEXT_CLEANING_ENABLED:
        cleaned = await clean_query_text(user_message, input_type=chat_request.input_type)
        if cleaned and isinstance(cleaned, str):
            await _update_user_message(chat_request.messages, cleaned)
            user_message = cleaned

    if Config.KOREAN_STANDARDIZATION_ENABLED:
        converted = await convert_korean_to_standard(user_message)
        if converted and isinstance(converted, str):
            await _update_user_message(chat_request.messages, converted)

    return user_message


def _save_stream_history(
    chat_request: ChatRequest,
    original_user_message: str,
    assistant_content: str,
    preprocess: dict = None,
    referenced_documents: list = None,
) -> None:
    """스트리밍 응답 완료 후 대화 히스토리를 저장합니다.

    merge 단계에서 채운 ``chat_request.messages``(전체 스레드)를 기준으로 저장한다.
    예전 구현은 DB ``get_history`` 결과만 이어 붙여 overwrite 했기 때문에, 조회가 빈 배열이면
    첫 턴이 사라지고 ``더 알려줘``(MORE_INFO) 직전 assistant/preprocess를 복구하지 못했다.
    """
    if not (original_user_message or assistant_content.strip()):
        return
    try:
        extra: dict = {}
        if isinstance(preprocess, dict) and preprocess:
            extra["preprocess"] = preprocess
        if referenced_documents is not None:
            extra["referenced_documents"] = referenced_documents
        _save_chat_history(
            chat_request,
            assistant_content,
            processed_user_message=original_user_message,
            user_message=original_user_message,
            **extra,
        )
        logger.info(
            "[History Saved/stream] user_id=%s, conv_id=%s",
            chat_request.user_id,
            chat_request.conv_id,
        )
    except Exception as save_err:
        logger.error(f"히스토리 저장 실패: {save_err}", exc_info=True)
