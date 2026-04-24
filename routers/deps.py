"""
Common helper functions shared across routers.
"""

import json
import logging
import asyncio
import uuid
from datetime import datetime
from typing import Any, Dict, Optional, AsyncGenerator

import mysql.connector

from fastapi.responses import JSONResponse

from core.config import Config
from core.models import ChatRequest
from core.constants import (
    ROLE_ASSISTANT,
    STREAM_CHUNK_DELAY,
    STREAM_FINISH_MARKER,
)
from services.chat_history_service import get_chat_history_service
from services.document_service import get_document_service
from utils import (
    extract_user_message,
    create_error_detail,
)

logger = logging.getLogger(__name__)
ANONYMOUS_HISTORY_USER_ID = "__anonymous__"
INSUFFICIENT_INFO_PATTERNS = (
    "제공된 정보만으로는 해당 내용을 안내하기 어렵습니다",
)


# ============================================================================
# DB Connection
# ============================================================================

def _get_db_connection():
    """Create and return a MySQL database connection."""
    return mysql.connector.connect(
        host=Config.DB_HOST,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD,
        database=Config.DB_NAME,
        port=Config.DB_PORT,
        autocommit=True,
        connection_timeout=Config.DB_CONNECTION_TIMEOUT,
    )


# ============================================================================
# Conversation helpers
# ============================================================================

def ensure_conversation_exists(conv_id, user_id, messages):
    """
    gsnd_conversations 테이블에 대화방이 없으면 생성, 있으면 updated_at 갱신.
    title이 기본값(Config.DEFAULT_CONVERSATION_TITLE)인 경우에만 첫 사용자 메시지로 업데이트.
    """
    if not user_id:
        return  # 비로그인 사용자는 skip

    title_msg = next(
        (msg.get('content', '').strip() for msg in messages if msg.get('role') == 'user'),
        Config.DEFAULT_CONVERSATION_TITLE
    )
    title = (title_msg[:50] + '...') if len(title_msg) > 50 else title_msg
    now = datetime.now()

    conn = _get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute(
            """INSERT INTO gsnd_conversations (id, user_id, title, created_at, updated_at)
               VALUES (%s, %s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE
                   title = IF(title = %s, VALUES(title), title),
                   updated_at = VALUES(updated_at)""",
            (conv_id, user_id, title, now, now, Config.DEFAULT_CONVERSATION_TITLE)
        )
        cursor.close()
    finally:
        conn.close()


def _resolve_history_user_id(user_id: Optional[str]) -> str:
    """
    Resolve user identifier used for chat-history persistence.

    UI conversation listing still uses real user_id, but history rows can be
    persisted with a fallback identifier when user_id is missing.
    """
    if isinstance(user_id, str) and user_id.strip():
        return user_id.strip()
    return ANONYMOUS_HISTORY_USER_ID


# ============================================================================
# LLM / Request helpers
# ============================================================================

def _build_llm_kwargs(chat_request: "ChatRequest") -> Dict[str, Any]:
    """Extract common LLM call parameters from a ChatRequest."""
    return {
        "messages": chat_request.messages,
        "temperature": chat_request.temperature,
        "max_tokens": chat_request.max_completion_tokens,
        "frequency_penalty": chat_request.frequency_penalty,
        "repetition_penalty": getattr(chat_request, 'repetition_penalty', 1.0),
        "top_p": chat_request.top_p,
        "top_k": getattr(chat_request, 'top_k', 1),
        "seed": chat_request.seed,
        "tools": chat_request.tools,
    }


def _validate_request(data: Dict[str, Any]) -> tuple[bool, Optional[JSONResponse]]:
    """
    Validate incoming chat request.

    Args:
        data: Raw request data

    Returns:
        Tuple of (is_valid, error_response)
    """
    if not data:
        return False, JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["body"],
                    msg="요청 본문이 없습니다",
                    type_="value_error"
                )]
            },
            status_code=400,
        )

    try:
        chat_request = ChatRequest(**data)
    except Exception as e:
        return False, JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["body"],
                    msg=str(e),
                    type_="validation_error"
                )]
            },
            status_code=400,
        )

    return True, None


def _validate_user_message(messages: list) -> tuple[bool, Optional[JSONResponse], str]:
    """
    Validate and extract user message from messages list.

    Args:
        messages: List of message dictionaries

    Returns:
        Tuple of (is_valid, error_response, user_message)
    """
    user_message = extract_user_message(messages)
    if not user_message:
        return False, JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["messages"],
                    msg="사용자 메시지가 없습니다",
                    type_="value_error"
                )]
            },
            status_code=400,
        ), ""

    return True, None, user_message


async def _update_user_message(messages: list, new_message: str) -> None:
    """
    Update the last user message in the message list.

    Args:
        messages: Message list to update
        new_message: New message content
    """
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            messages[i]["content"] = new_message
            break


def _save_chat_history(chat_request: ChatRequest, assistant_message: str, processed_user_message: str = None, **kwargs) -> Optional[str]:
    """
    Save chat history. Auto-generates conv_id if missing.

    Args:
        chat_request: Chat request object
        assistant_message: Assistant response message
        processed_user_message: Processed user message after cleaning and standardization (for title generation)

    Returns:
        conv_id (newly created or existing)
    """

    user_message = kwargs.get('user_message') or processed_user_message

    user_id = chat_request.user_id
    conv_id = chat_request.conv_id

    # 누적 저장: 기존 대화내역(chat_request.messages)에 마지막 assistant 응답 추가
    messages = list(chat_request.messages) if chat_request.messages else []

    # 마지막 user 메시지를 원본 질문으로 덮어쓰기
    if user_message:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                messages[i]["content"] = user_message
                break

    # 마지막 user/assistant 쌍이 중복이 아닐 때만 assistant 응답 추가
    already_saved = (
        len(messages) >= 2
        and messages[-2].get("role") == "user"
        and messages[-1].get("role") == ROLE_ASSISTANT
        and messages[-2].get("content") == user_message
        and messages[-1].get("content") == assistant_message
    )
    if not already_saved:
        if messages and messages[-1].get("role") == ROLE_ASSISTANT:
            # 마지막이 이미 assistant이면 내용 덮어쓰기
            messages[-1]["content"] = assistant_message
        else:
            messages.append({"role": ROLE_ASSISTANT, "content": assistant_message})


    logger.info(f"[_save_chat_history] user_id={user_id}, conv_id={conv_id}")

    if not conv_id:
        conv_id = str(uuid.uuid4())
        chat_request.conv_id = conv_id
        logger.info(f"[_save_chat_history] conv_id 자동 생성: {conv_id}")

    persist_user_id = _resolve_history_user_id(user_id)

    history_service = get_chat_history_service(Config)
    history_service.upsert_history(user_id=persist_user_id, conv_id=conv_id, messages=messages, overwrite=True)

    # 대화방 row 생성 (중복이면 IGNORE)
    title_source = processed_user_message or next(
        (m.get('content', '').strip() for m in chat_request.messages if isinstance(m, dict) and m.get('role') == 'user'),
        Config.DEFAULT_CONVERSATION_TITLE
    ) or Config.DEFAULT_CONVERSATION_TITLE
    try:
        ensure_conversation_exists(conv_id, persist_user_id, [{"role": "user", "content": title_source}])
    except Exception as e:
        logger.error(f"Error auto-creating conversation: {e}")

    return conv_id




def _enrich_referenced_documents(docs: Optional[list]) -> list:
    """Enrich referenced documents with dataset IDs using document names."""
    if not docs:
        return []

    names = [d.get("name") for d in docs if isinstance(d, dict) and d.get("name")]
    if not names:
        return []

    doc_service = get_document_service(Config)
    db_docs = doc_service.get_documents_by_names(names)
    id_by_name = {d.get("name"): d.get("id") for d in db_docs if d.get("name")}

    enriched = []
    for d in docs:
        if not isinstance(d, dict):
            continue
        name = d.get("name")
        if not name:
            continue
        enriched.append({
            "name": name,
            "id": id_by_name.get(name, d.get("id", "")) or "",
            "chunk_id": d.get("chunk_id", "") or "",
            "snippet": d.get("snippet", "") or "",
            "path": d.get("path", "") or "",
        })
    return enriched


def _normalize_for_match(text: str) -> str:
    """비교용 정규화: 공백·특수문자 제거, 소문자 변환"""
    import re
    return re.sub(r'[\s\-·•()（）]', '', text).lower()


def _is_doc_mentioned_in_response(doc_name: str, response: str, norm_response: str) -> bool:
    """문서명이 응답 텍스트에 언급되었는지 판단 (4단계 전략)"""
    if not doc_name:
        return False
    # 전략 1: 원문 부분 매칭
    if doc_name in response:
        return True
    # 전략 2: 정규화 후 부분 매칭 (4자 이상)
    norm_name = _normalize_for_match(doc_name)
    if len(norm_name) >= 4 and norm_name in norm_response:
        return True
    # 전략 3: 앞 10자 prefix 매칭
    if len(doc_name) > 10 and doc_name[:10] in response:
        return True
    # 전략 4: 파일명 패턴(YYYY_시군명_서비스명.확장자)에서 서비스명만 추출 후 매칭
    import re as _re
    service_part = _re.sub(r'^\d{4}_[^_]+_', '', doc_name)   # "2026_밀양시_" 제거
    service_part = _re.sub(r'\.\w+$', '', service_part)        # ".hwpx" 등 확장자 제거
    if service_part and service_part != doc_name and service_part in response:
        return True
    return False


def _filter_referenced_documents_by_response(response_content: str, docs: Optional[list]) -> list:
    """참조 문서를 전체 반환."""
    if not docs:
        return []
    return docs


# ============================================================================
# Streaming helpers
# ============================================================================

def _build_streaming_chunk(
    chunk_id: str,
    created: int,
    model: str,
    content: str
) -> str:
    """
    Build a single streaming chunk in SSE format.

    Args:
        chunk_id: Unique chunk ID
        created: Creation timestamp
        model: Model name
        content: Chunk content

    Returns:
        Formatted SSE data string
    """
    chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content},
                "finish_reason": None
            }
        ]
    }
    return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"


async def _build_streaming_response(
    content: str,
    response_template: Dict[str, Any]
) -> AsyncGenerator[str, None]:
    """
    Build streaming response by yielding chunks character by character.

    Args:
        content: Response content to stream
        response_template: Response template with id, created, model

    Yields:
        SSE formatted data strings
    """
    chunk_id = response_template["id"]
    created = response_template["created"]
    model = response_template["model"]

    for char in content:
        yield _build_streaming_chunk(chunk_id, created, model, char)
        await asyncio.sleep(STREAM_CHUNK_DELAY)

    yield f"data: {STREAM_FINISH_MARKER}\n\n"


async def _stream_delta_content(content: str) -> AsyncGenerator[str, None]:
    for char in content:
        chunk_data = {
            "choices": [{
                "index": 0,
                "delta": {"content": char},
                "finish_reason": None
            }]
        }
        yield f"data: {json.dumps(chunk_data, ensure_ascii=False)}\n\n"
        await asyncio.sleep(STREAM_CHUNK_DELAY)
