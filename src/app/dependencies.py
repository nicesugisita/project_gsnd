"""
공유 FastAPI 의존성 및 헬퍼.

Phase 1 providers (app.state에서 꺼내 반환):
  get_llm_client()  — httpx.AsyncClient (LLM)
  get_ds_client()   — httpx.AsyncClient (DeepServer)
  get_db()          — DB 커넥션 (풀 or 단건)
  get_repository()  — ChatHistoryRepository
  get_retriever()   — MarinerRetriever

사용법:
    from fastapi import Depends
    from app.dependencies import get_llm_client, get_repository, get_retriever

    @router.post("/v1/chat/...")
    async def endpoint(
        llm_client = Depends(get_llm_client),
        repo       = Depends(get_repository),
        retriever  = Depends(get_retriever),
    ):
        ...
"""

import logging
import re
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Generator, Optional

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse

from app.core.config import Config
from app.shared.db.connection import get_db_connection

if TYPE_CHECKING:
    from app.chat.infra.retriever import MarinerRetriever
    from app.conversation.repository import ChatHistoryRepository
from app.shared.schemas import ChatRequest
from app.core.constants import ROLE_ASSISTANT
from app.conversation.history import get_chat_history_service
from app.shared.utils import (
    extract_user_message,
    create_error_detail,
)
from app.chat._doc_filter import (  # noqa: F401  (re-export for callers)
    _enrich_referenced_documents,
    _filter_referenced_documents_by_response,
)
from app.chat._stream_utils import (  # noqa: F401  (re-export for callers)
    _build_streaming_response,
    _stream_delta_content,
)

logger = logging.getLogger(__name__)
ANONYMOUS_HISTORY_USER_ID = "__anonymous__"

_UUID_V4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_DEFAULT_CHAT_EPHEMERAL_PREFIXES: tuple[str, ...] = (
    "temp-",
    "guest-",
    "anon-",
    "visitor-",
    "session-",
    "device-",
    "anonymous-",
)


def chat_ephemeral_user_id_prefixes() -> tuple[str, ...]:
    raw = (getattr(Config, "CHAT_EPHEMERAL_USER_ID_PREFIXES", None) or "").strip()
    if raw:
        return tuple(x.strip().lower() for x in raw.split(",") if x.strip())
    return _DEFAULT_CHAT_EPHEMERAL_PREFIXES


def chat_user_id_requires_nologin_canonical(user_id: Optional[str]) -> bool:
    """채팅 히스토리 행 소유자를 비로그인 canonical(``conv_id``)로 고정해야 하는 입력인지."""

    if not isinstance(user_id, str):
        return True
    s = user_id.strip()
    if not s:
        return True
    if s == ANONYMOUS_HISTORY_USER_ID:
        return True
    low = s.lower()
    if low in ("anonymous", "guest", "unknown", "null", "undefined"):
        return True
    for p in chat_ephemeral_user_id_prefixes():
        if low.startswith(p):
            return True
    if getattr(Config, "CHAT_USER_ID_IS_EPHEMERAL_IF_UUIDV4", False) and bool(_UUID_V4.match(s)):
        return True
    return False


# ============================================================================
# Phase 1 — Provider functions (app.state에서 꺼내 반환)
# ============================================================================

def get_llm_client(request: Request) -> httpx.AsyncClient:
    """
    LLM httpx.AsyncClient를 app.state에서 반환.

    테스트 시 dependency_overrides로 Mock 교체:
        app.dependency_overrides[get_llm_client] = lambda: MockLLMClient()
    """
    client: httpx.AsyncClient | None = getattr(request.app.state, "llm_client", None)
    if client is None or client.is_closed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM 클라이언트가 초기화되지 않았습니다.",
        )
    return client


def get_ds_client(request: Request) -> httpx.AsyncClient:
    """DeepServer httpx.AsyncClient를 app.state에서 반환."""
    client: httpx.AsyncClient | None = getattr(request.app.state, "ds_client", None)
    if client is None or client.is_closed:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="DeepServer 클라이언트가 초기화되지 않았습니다.",
        )
    return client


def get_retriever(request: Request) -> "MarinerRetriever":
    """
    MarinerRetriever를 app.state에서 반환.

    JVM 초기화 후 lifespan에서 app.state.retriever에 저장된 인스턴스.
    """
    retriever = getattr(request.app.state, "retriever", None)
    if retriever is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Mariner retriever가 초기화되지 않았습니다.",
        )
    return retriever


# ============================================================================
# DB Connection
# ============================================================================

def get_db(request: Request) -> Generator:
    """
    FastAPI Depends() 용 DB 커넥션 의존성.

    app.state.db_pool(MySQLConnectionPool)이 있으면 풀에서 커넥션을 가져오고,
    없으면 단건 연결로 폴백한다. 요청 처리 후 자동으로 커넥션을 반환/닫는다.

    사용법:
        from fastapi import Depends
        from app.dependencies import get_db

        @router.get("/...")
        def endpoint(conn = Depends(get_db)):
            cursor = conn.cursor()
            ...
    """
    pool = getattr(request.app.state, "db_pool", None)
    if pool is not None:
        conn = pool.get_connection()
        try:
            yield conn
        finally:
            conn.close()  # 풀에 반환
    else:
        conn = get_db_connection()
        try:
            yield conn
        finally:
            conn.close()


def get_suggest_questions_service(
    llm_client: httpx.AsyncClient = Depends(get_llm_client),
) -> "SuggestQuestionsService":
    """
    SuggestQuestionsService를 LLM 클라이언트와 함께 생성.

    테스트 Mock 교체 예시:
        class MockService:
            async def generate(self, user_query, assistant_response, max_questions=5):
                return ["Mock Q1?", "Mock Q2?"]

        app.dependency_overrides[get_suggest_questions_service] = lambda: MockService()
    """
    from app.chat.suggest_questions_service import SuggestQuestionsService
    return SuggestQuestionsService(client=llm_client)


def get_repository(db=Depends(get_db)) -> "ChatHistoryRepository":
    """
    ChatHistoryRepository를 주입받은 DB 커넥션으로 생성.

    테스트 시 dependency_overrides로 Mock 교체:
        app.dependency_overrides[get_repository] = lambda: MockRepository()
    """
    from app.conversation.repository import ChatHistoryRepository
    return ChatHistoryRepository(db)


def _get_db_connection():
    """하위 호환: 기존 동기 코드용 단건 DB 연결."""
    return get_db_connection()


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


def _resolve_chat_history_user_id(
    user_id: Optional[str], conv_id: Optional[str]
) -> str:
    """
    /v1/chat/completions 과 동일한 규칙으로 히스토리 행 소유자 ID를 만든다.

    로그인 user_id 문자열은 그대로 두고, 비어 있거나 플레이스홀더·임시 접두어 또는
    잘못된 ``nologin…`` 문자열은 ``conv_id`` 로 맞춘다.
    """
    cid = str(conv_id or "").strip()
    canonical = cid if cid else ""

    uid_raw = user_id.strip() if isinstance(user_id, str) and user_id.strip() else ""
    if not uid_raw:
        if cid:
            return canonical
        return ANONYMOUS_HISTORY_USER_ID

    if chat_user_id_requires_nologin_canonical(uid_raw):
        if cid:
            return canonical
        return ANONYMOUS_HISTORY_USER_ID

    if uid_raw.startswith("nologin"):
        if cid:
            return canonical
        return uid_raw

    return uid_raw


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
        # 클라이언트가 seed를 명시 null로 보내도 재현성 보장 (생략 시엔 스키마 기본 42)
        "seed": chat_request.seed if chat_request.seed is not None else Config.FIXED_LLM_SEED,
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


def merge_assistant_reference_docs(msg: dict, referenced_documents: list) -> None:
    """RAG/API 참조를 assistant에 반영. DB 저장 시 snippet·path는 비워 messages_json 크기를 줄인다."""
    if not isinstance(msg, dict):
        return
    if not referenced_documents:
        msg.pop("referenced_chunk_ids", None)
        msg.pop("referenced_service_names", None)
        md = msg.get("metadata")
        if isinstance(md, dict):
            md = dict(md)
            md.pop("referenced_documents", None)
            if md:
                msg["metadata"] = md
            else:
                msg.pop("metadata", None)
        else:
            msg.pop("metadata", None)
        return

    chunk_ids: list[str] = []
    service_names: list[str] = []
    meta_docs: list[dict[str, Any]] = []
    for d in referenced_documents:
        if not isinstance(d, dict):
            continue
        cid = str(d.get("chunk_id") or d.get("id") or "").strip()
        name = str(d.get("name") or "").strip()
        meta_docs.append({
            "chunk_id": cid,
            "id": str(d.get("id") or cid),
            "name": name,
            "snippet": "",
            "path": "",
        })
        if cid:
            chunk_ids.append(cid)
        if name:
            service_names.append(name)

    prev_md = msg.get("metadata")
    md = dict(prev_md) if isinstance(prev_md, dict) else {}
    md["referenced_documents"] = meta_docs
    msg["metadata"] = md
    msg["referenced_chunk_ids"] = chunk_ids
    msg["referenced_service_names"] = service_names


def _apply_assistant_enrichments(msg: dict, kwargs: dict) -> None:
    if "preprocess" in kwargs:
        p = kwargs["preprocess"]
        if isinstance(p, dict) and p:
            prev = msg.get("preprocess")
            base = dict(prev) if isinstance(prev, dict) else {}
            base.update(p)
            base.pop("intent_reason", None)
            msg["preprocess"] = base
        elif p is None or p == {}:
            msg.pop("preprocess", None)
    if "referenced_documents" in kwargs:
        rd = kwargs["referenced_documents"]
        if rd is None:
            rd = []
        merge_assistant_reference_docs(msg, rd if isinstance(rd, list) else [])
    # 추천 카드(서비스 카드/주제 칩/슬롯): 라이브 응답(build_chat_response)과 동일한
    # 최상위 키로 영속 → 대화 복원 시 그대로 재현. 값이 없으면(None/빈값) 키 제거.
    for _k in ("guide_services", "topic_chips", "slots"):
        if _k in kwargs:
            _v = kwargs[_k]
            if _v:
                msg[_k] = _v
            else:
                msg.pop(_k, None)


def _save_chat_history(chat_request: ChatRequest, assistant_message: str, processed_user_message: str = None, **kwargs) -> Optional[str]:
    """
    Save chat history. Auto-generates conv_id if missing.

    Args:
        chat_request: Chat request object
        assistant_message: Assistant response message
        processed_user_message: Processed user message after cleaning and standardization (for title generation)
        kwargs: user_message, preprocess(dict), referenced_documents(list),
                guide_services(list), topic_chips(list), slots(dict) — DB 복원·MORE_INFO 제외용 메타

    Returns:
        conv_id (newly created or existing)
    """

    user_message = kwargs.get('user_message') or processed_user_message
    enrich_kw = {
        k: kwargs[k]
        for k in ("preprocess", "referenced_documents", "guide_services", "topic_chips", "slots")
        if k in kwargs
    }
    has_enrichments = bool(enrich_kw)

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
        not has_enrichments
        and len(messages) >= 2
        and messages[-2].get("role") == "user"
        and messages[-1].get("role") == ROLE_ASSISTANT
        and messages[-2].get("content") == user_message
        and messages[-1].get("content") == assistant_message
    )
    if not already_saved:
        if messages and messages[-1].get("role") == ROLE_ASSISTANT:
            messages[-1]["content"] = assistant_message
            _apply_assistant_enrichments(messages[-1], enrich_kw)
        else:
            entry = {"role": ROLE_ASSISTANT, "content": assistant_message}
            _apply_assistant_enrichments(entry, enrich_kw)
            messages.append(entry)
    elif has_enrichments and messages and messages[-1].get("role") == ROLE_ASSISTANT:
        messages[-1]["content"] = assistant_message
        _apply_assistant_enrichments(messages[-1], enrich_kw)


    logger.info(f"[_save_chat_history] user_id={user_id}, conv_id={conv_id}")

    if not conv_id:
        conv_id = str(uuid.uuid4())
        chat_request.conv_id = conv_id
        logger.info(f"[_save_chat_history] conv_id 자동 생성: {conv_id}")

    persist_user_id = _resolve_chat_history_user_id(user_id, conv_id)

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




