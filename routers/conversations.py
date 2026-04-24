"""
Conversation management endpoints.
"""

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from core.config import Config
from services.chat_history_service import get_chat_history_service
from services.conversation_service import get_conversation_service
from services.message_feedback_service import get_message_feedback_service
from utils import create_error_detail
from .deps import _resolve_history_user_id

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get('/v1/chat/conversations')
async def get_conversations(user_id: str):
    """Get all conversations for a user."""
    if not user_id:
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["query"],
                    msg="user_id is required",
                    type_="value_error",
                )]
            },
            status_code=400,
        )

    conv_service = get_conversation_service(Config)
    conversations = conv_service.get_conversations(user_id=user_id)
    return JSONResponse(
        content={"conversations": conversations},
        status_code=200,
    )


@router.post('/v1/chat/conversations')
async def create_conversation(request: Request):
    """Create a new conversation."""
    data = await request.json()
    user_id = data.get("user_id")
    title = data.get("title", Config.DEFAULT_CONVERSATION_TITLE)

    if not user_id:
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["body"],
                    msg="user_id is required",
                    type_="value_error",
                )]
            },
            status_code=400,
        )

    conv_service = get_conversation_service(Config)
    conv_id = conv_service.create_conversation(user_id=user_id, title=title)

    # 새 대화 생성 시 인사말을 첫 번째 assistant 메시지로 저장
    greeting_message = {
        "role": "assistant",
        "content": Config.GREETING_MESSAGE,
    }
    history_service = get_chat_history_service(Config)
    history_service.upsert_history(user_id=user_id, conv_id=conv_id, messages=[greeting_message])

    return JSONResponse(
        content={"conv_id": conv_id, "title": title},
        status_code=201,
    )


@router.get('/v1/chat/conversations/{conv_id}/messages')
async def get_conversation_messages(conv_id: str):
    """Get messages in a specific conversation."""

    history_service = get_chat_history_service(Config)
    messages = history_service.get_history(conv_id=conv_id)

    # 조항 : assistant 메시지에 action 가이드를 추가하여 클라이언트가 버튼을 띄우게 함
    for msg in messages:
        if msg.get("role") == "assistant":
            # 1. action 키가 없거나 dict가 아니면 초기화
            if "action" not in msg or not isinstance(msg["action"], dict):
                msg["action"] = {}

            # 2. 필드 채우기
            msg["action"]["type"] = Config.ASSISTANT_ACTION_FEEDBACK
            msg["action"]["options"] = Config.ASSISTANT_ACTION_FEEDBACK_OPTIONS

            # 3. 만약 metadata에 이미 reaction 데이터가 있다면 action으로 복사 (하위호환성용)
            metadata = msg.get("metadata")
            if isinstance(metadata, dict):
                feedback_data = metadata.get("feedback", {})
                if isinstance(feedback_data, dict) and feedback_data.get("reaction"):
                    msg["action"]["reaction"] = feedback_data["reaction"]

    return JSONResponse(
        content={"conv_id": conv_id, "messages": messages},
        status_code=200,
    )


@router.delete('/v1/chat/conversations/{conv_id}')
async def delete_conversation(conv_id: str, user_id: str):
    """Delete a conversation."""
    if not conv_id or not user_id:
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["path" if not conv_id else "query"],
                    msg="conv_id and user_id are required",
                    type_="value_error",
                )]
            },
            status_code=400,
        )

    conv_service = get_conversation_service(Config)
    success = conv_service.delete_conversation(conv_id=conv_id, user_id=user_id)

    if not success:
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["path"],
                    msg="Conversation not found or permission denied",
                    type_="not_found",
                )]
            },
            status_code=404,
        )

    return JSONResponse(
        content={"success": True},
        status_code=200,
    )


@router.post('/v1/chat/messages/feedback')
async def save_message_feedback(request: Request):
    """Save copy/like/dislike feedback for an assistant message."""
    data = await request.json()

    raw_user_id = data.get("user_id")
    user_id = raw_user_id.strip() if isinstance(raw_user_id, str) and raw_user_id.strip() else None
    persist_user_id = _resolve_history_user_id(user_id)
    conv_id = (data.get("conv_id") or "").strip()
    action = (data.get("action") or "").strip().lower()
    message_index = data.get("message_index")

    valid_actions = {"copy", "like", "dislike", "clear"}
    if not conv_id or message_index is None or action not in valid_actions:
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["body"],
                    msg="conv_id, message_index, action(copy|like|dislike|clear) are required",
                    type_="value_error",
                )]
            },
            status_code=400,
        )

    if not isinstance(message_index, int) or message_index < 0:
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["body", "message_index"],
                    msg="message_index must be a non-negative integer",
                    type_="value_error",
                )]
            },
            status_code=400,
        )

    feedback = {}
    history_updated = False
    can_update_history = False
    metadata = None
    target_message = None
    messages = []

    history_service = get_chat_history_service(Config)
    # 기존 히스토리를 가져와서 message_index에 해당하는 assistant 메시지에 feedback 반영 후 전체 히스토리 업데이트
    messages = history_service.get_history(conv_id=conv_id)

    # 사용자가 찍은 인덱스를 찾아서 수정
    if 0 <= message_index < len(messages):
        target_message = messages[message_index]

        if target_message.get("role") == "assistant":
            target_message["action"] = {"type":"feedback", "reaction":action}
            messages[message_index] = target_message

    # 수정된 전체리스트를 overwrite=True 로 통쨰로 덮어씌우기
    history_service.upsert_history(user_id=persist_user_id, conv_id=conv_id, messages=messages, overwrite=True)

    # 별도 피드백 테이블 전송 (통계용)
    feedback_service = get_message_feedback_service(Config)
    feedback_saved = feedback_service.upsert_feedback(
        user_id=user_id,
        conv_id=conv_id,
        message_index=message_index,
        action=action,
    )

    return JSONResponse(
        content={
            "success": True,
            "feedback_saved": feedback_saved,
            "history_updated": history_updated,
            "message_index": message_index,
            "action": action,
            "feedback": feedback,
        },
        status_code=200,
    )


@router.get('/v1/chat/history')
async def chat_history(conv_id: str):
    """
    Get chat history for a conversation (deprecated - use /conversations/{conv_id}/messages).
    """
    if not conv_id:
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["query"],
                    msg="conv_id is required",
                    type_="value_error",
                )]
            },
            status_code=400,
        )

    history_service = get_chat_history_service(Config)
    messages = history_service.get_history(conv_id=conv_id)
    return JSONResponse(
        content={
            "conv_id": conv_id,
            "messages": messages,
        },
        status_code=200,
    )
