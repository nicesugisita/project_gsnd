"""채팅 라우트 핸들러"""

import asyncio
import logging
import time
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.config import Config
from core.models import ChatRequest, RecommendStartRequest
from core.logging_context import set_log_context, reset_log_context
from services import convert_korean_to_standard, generate_suggested_questions
from utils import is_clarification_answer
from services.sigun_service import (
    count_sigun_ask_attempts,
    MSG_SIGUN_FAILURE,
    MSG_OUT_OF_SCOPE_TEMPLATE,
    MAX_SIGUN_ASK_ATTEMPTS,
)
from utils import build_chat_response, create_error_detail
from ..deps import (
    _save_chat_history,
    _update_user_message,
    _validate_request,
    _validate_user_message,
)
from ._stream_utils import _build_streaming_response
from .conversation import _check_user_limit, _merge_and_init_conversation
from .helpers import (
    _handle_clarify_response,
    _handle_no_rag_mode,
    _handle_rag_mode,
    _run_query_recreation,
)
from ._pipeline_steps import (
    run_pre_check,
    run_early_sigun_check,
    run_out_of_scope_check,
    run_sigun_check,
    run_unified_preprocess,
    run_lifecycle_check,
)
from .streaming import _streaming_chat_flow

logger = logging.getLogger(__name__)

router = APIRouter()


def _ensure_runtime_user_id(chat_request: ChatRequest) -> None:
    """비로그인 요청에도 로그인과 동일 경로를 타도록 임시 user_id를 부여."""
    if isinstance(chat_request.user_id, str) and chat_request.user_id.strip():
        return

    if not chat_request.conv_id:
        chat_request.conv_id = str(uuid.uuid4())

    # 요구사항: 비로그인 user_id는 'nologin+conv_id' 형태로 고정
    chat_request.user_id = f"nologin{chat_request.conv_id}"


@router.post('/v1/chat/completions')
async def chat_completions(request: Request):
    """Chat Completions API endpoint."""
    log_context_tokens = None
    t_request_start = time.monotonic()
    origin = request.headers.get("origin")
    logger.info(f"[Chat Completions] origin={origin}")
    try:
        data: Dict[str, Any] = await request.json()

        is_valid, error_response = _validate_request(data)
        if not is_valid:
            return error_response

        chat_request = ChatRequest(**data)
        _ensure_runtime_user_id(chat_request)
        log_context_tokens = set_log_context(chat_request.conv_id, chat_request.user_id)
        first_msg_preview = chat_request.messages[0].get('content', '')[:50] if chat_request.messages else 'None'
        logger.info(f"[Chat Request] user_id={chat_request.user_id}, conv_id={chat_request.conv_id}, stream={data.get('stream')}, 첫 메시지: {first_msg_preview}")
        await _merge_and_init_conversation(chat_request)

        is_valid, error_response, user_message = _validate_user_message(chat_request.messages)
        if not is_valid:
            return error_response

        original_user_message = user_message
        stream = data.get("stream", False)

        is_clarification = is_clarification_answer(chat_request.messages)

        if not is_clarification:
            limit_response = _check_user_limit(user_message, chat_request, stream)
            if limit_response:
                return limit_response

        if chat_request.mode == "guide_recommend":
            if Config.KOREAN_STANDARDIZATION_ENABLED:
                converted = await convert_korean_to_standard(user_message)
                if converted and isinstance(converted, str):
                    await _update_user_message(chat_request.messages, converted)
            return await _handle_rag_mode(
                user_message,
                chat_request,
                chat_request.conv_id,
                stream,
                intent="guide_recommend"
            )

        if stream:
            return StreamingResponse(
                _streaming_chat_flow(chat_request, user_message, original_user_message, is_clarification, t_request_start=t_request_start),
                media_type="text/event-stream"
            )

        # Non-streaming flow
        # [1] PreCheck: RAG 여부 + 되묻기
        pre = await run_pre_check(user_message, chat_request.messages, is_clarification)
        use_rag = pre.use_rag
        if pre.clarification_question:
            await asyncio.to_thread(_save_chat_history, chat_request, pre.clarification_question, original_user_message)
            return await _handle_clarify_response(original_user_message, pre.clarification_question, chat_request, stream)

        # [2] 되묻기 답변의 시군 조기 확인 (query_recreation LLM 호출 전)
        resolved_sigun_filters = None
        early_sigun = run_early_sigun_check(user_message, chat_request.messages, use_rag, is_clarification)
        if early_sigun is not None:
            if early_sigun.need_clarify:
                if count_sigun_ask_attempts(chat_request.messages) >= MAX_SIGUN_ASK_ATTEMPTS:
                    return JSONResponse(content=build_chat_response(response_message=MSG_SIGUN_FAILURE, user_message=user_message, model_name=Config.MODEL_NAME), status_code=200)
                return await _handle_clarify_response(original_user_message, early_sigun.ask_message, chat_request, stream)
            resolved_sigun_filters = early_sigun.filters
            logger.info("[SigunCheck/non-stream] 조기 확정(is_clarification): %s", resolved_sigun_filters)

        # [3] 쿼리 재구성
        user_message, skip_clarification_check = await _run_query_recreation(user_message, chat_request, is_clarification)

        # [4] 경상남도 외 지역 체크
        out_of_scope, region_name = run_out_of_scope_check(user_message, use_rag)
        if out_of_scope:
            msg = MSG_OUT_OF_SCOPE_TEMPLATE.format(region=region_name)
            await asyncio.to_thread(_save_chat_history, chat_request, msg, original_user_message)
            return JSONResponse(content=build_chat_response(response_message=msg, user_message=user_message, model_name=Config.MODEL_NAME), status_code=200)

        # [5] 시군 체크
        sigun = run_sigun_check(user_message, chat_request.messages, use_rag, resolved_sigun_filters)
        if sigun is not None:
            if sigun.need_clarify:
                if count_sigun_ask_attempts(chat_request.messages) >= MAX_SIGUN_ASK_ATTEMPTS:
                    return JSONResponse(content=build_chat_response(response_message=MSG_SIGUN_FAILURE, user_message=user_message, model_name=Config.MODEL_NAME), status_code=200)
                return await _handle_clarify_response(original_user_message, sigun.ask_message, chat_request, stream)
            resolved_sigun_filters = sigun.filters
            logger.info("[SigunCheck/non-stream] sigun_filters=%s", resolved_sigun_filters)

        # [6] 통합 전처리
        _preprocess_messages = None if skip_clarification_check else chat_request.messages
        preprocess = await run_unified_preprocess(user_message, _preprocess_messages, use_rag)
        user_message = preprocess.query
        await _update_user_message(chat_request.messages, user_message)

        # [7] 생애주기 체크 (guide_recommend 전용, 로그만)
        run_lifecycle_check(user_message, chat_request.messages, use_rag, preprocess.intent)

        if not use_rag:
            return await _handle_no_rag_mode(user_message, chat_request)

        return await _handle_rag_mode(
            user_message,
            chat_request,
            chat_request.conv_id,
            stream,
            intent=preprocess.intent,
            sigun_filters=resolved_sigun_filters,
            reformed_query=preprocess.reformed_query,
            expanded_queries=preprocess.expanded_queries,
            keywords=preprocess.keywords,
        )

    except Exception as e:
        logger.error(f"Chat completions error: {e}", exc_info=True)
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["body"],
                    msg=str(e),
                    type_="internal_error",
                )]
            },
            status_code=500,
        )
    finally:
        if log_context_tokens is not None:
            reset_log_context(log_context_tokens)


@router.post('/v1/chat/recommend/collect')
async def collect_recommendation_inputs(body: RecommendStartRequest = Body(default=RecommendStartRequest())):
    """복지서비스추천 버튼 트리거: 지역·출생연도 수집 질문을 반환합니다."""
    QUESTION = "거주 지역(시.군)과 출생연도를 알려주세요."
    conv_id = body.conv_id or str(uuid.uuid4())
    log_context_tokens = set_log_context(conv_id, body.user_id)

    try:
        response = build_chat_response(
            response_message=QUESTION,
            user_message="",
            model_name=Config.MODEL_NAME,
            conv_id=conv_id,
            is_clarification=True,
        )

        if body.stream:
            return StreamingResponse(
                _build_streaming_response(QUESTION, response),
                media_type="text/event-stream",
            )
        return JSONResponse(content={"message": QUESTION, "conv_id": conv_id}, status_code=200)
    finally:
        reset_log_context(log_context_tokens)


@router.post('/v1/chat/suggest-questions')
async def suggest_questions(request: Request):
    """Generate suggested follow-up questions based on user query and assistant response."""
    try:
        body = await request.json()
        user_query = body.get('user_query', '')
        assistant_response = body.get('assistant_response', '')
        is_clarification = body.get('is_clarification', False)

        if not user_query or not assistant_response:
            return JSONResponse(
                content={
                    "detail": [create_error_detail(
                        loc=["body"],
                        msg="user_query and assistant_response are required",
                        type_="value_error",
                    )]
                },
                status_code=400,
            )

        if is_clarification:
            return JSONResponse(content={"questions": []}, status_code=200)

        questions = await generate_suggested_questions(
            user_query=user_query,
            assistant_response=assistant_response,
            max_questions=5
        )
        return JSONResponse(content={"questions": questions}, status_code=200)

    except Exception as e:
        logger.error(f"Error generating suggested questions: {e}")
        return JSONResponse(
            content={
                "detail": [create_error_detail(
                    loc=["body"],
                    msg="Failed to generate suggested questions",
                    type_="internal_error",
                )]
            },
            status_code=500,
        )
