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
from services import (
    convert_korean_to_standard,
    generate_suggested_questions,
    unified_preprocess,
)
from services.llm_service.judgment import pre_check
from services.sigun_service import (
    check_sigun,
    check_out_of_scope_region,
    count_sigun_ask_attempts,
    MSG_SIGUN_FAILURE,
    MSG_OUT_OF_SCOPE_TEMPLATE,
    MAX_SIGUN_ASK_ATTEMPTS,
)
from services.lifecycle_service import check_lifecycle
from utils import (
    build_chat_response,
    create_error_detail,
)
from ..deps import (
    _build_streaming_response,
    _save_chat_history,
    _update_user_message,
    _validate_request,
    _validate_user_message,
)
from .conversation import (
    _check_user_limit,
    _merge_and_init_conversation,
)
from .helpers import (
    _handle_clarify_response,
    _handle_no_rag_mode,
    _handle_rag_mode,
    _run_query_recreation,
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
        print("===========================chat_request.messages===========================", chat_request.messages)

        first_msg_preview = chat_request.messages[0].get('content', '')[:50] if chat_request.messages else 'None'
        logger.info(f"[Chat Request] user_id={chat_request.user_id}, conv_id={chat_request.conv_id}, stream={data.get('stream')}, 첫 메시지: {first_msg_preview}")
        await _merge_and_init_conversation(chat_request)

        is_valid, error_response, user_message = _validate_user_message(chat_request.messages)
        if not is_valid:
            return error_response

        original_user_message = user_message
        stream = data.get("stream", False)

        from utils import is_clarification_answer
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
        # ── [1단계] 선행 판단: use_rag + 되묻기 (is_clarification이면 스킵) ──
        use_rag = True
        if not is_clarification:
            _t = time.monotonic()
            pre_check_result = await pre_check(user_message, chat_request.messages)
            logger.info("[TIMING] pre_check: %.3fs", time.monotonic() - _t)
            use_rag = pre_check_result["use_rag"]
            clarification_question = pre_check_result["clarification_question"]
            if clarification_question:
                await asyncio.to_thread(_save_chat_history, chat_request, clarification_question, original_user_message)
                return await _handle_clarify_response(
                    original_user_message, clarification_question, chat_request, stream
                )
        # ────────────────────────────────────────────────────────────────────

        # ── 되묻기 답변의 시군 조기 확인 (query_recreation LLM 호출 전) ──────
        # query_recreation LLM이 임의로 시군을 추측·삽입하는 것을 방지하기 위해
        # is_clarification=True일 때 반드시 먼저 시군을 확정하거나 재질문합니다.
        resolved_sigun_filters = None
        if is_clarification and use_rag:
            _pre_filters, _pre_need_ask, _pre_ask_msg = check_sigun(
                user_message, chat_request.messages
            )
            if _pre_need_ask:
                _ask_count = count_sigun_ask_attempts(chat_request.messages)
                if _ask_count >= MAX_SIGUN_ASK_ATTEMPTS:
                    response = build_chat_response(response_message=MSG_SIGUN_FAILURE, user_message=user_message, model_name=Config.MODEL_NAME)
                    return JSONResponse(content=response, status_code=200)
                return await _handle_clarify_response(original_user_message, _pre_ask_msg, chat_request, stream)
            # 시군 확정 → 이후 check_sigun 재실행 불필요
            resolved_sigun_filters = _pre_filters if _pre_filters else None
            logger.info(f"[SigunCheck/non-stream] 조기 확정(is_clarification): {resolved_sigun_filters}")
        # ─────────────────────────────────────────────────────────────────────

        user_message, skip_clarification_check = await _run_query_recreation(
            user_message, chat_request, is_clarification
        )

        # ── 경상남도 외 지역 체크 ─────────────────────────────────────────────
        if use_rag:
            out_of_scope, region_name = check_out_of_scope_region(user_message)
            if out_of_scope:
                msg = MSG_OUT_OF_SCOPE_TEMPLATE.format(region=region_name)
                response = build_chat_response(
                    response_message=msg,
                    user_message=user_message,
                    model_name=Config.MODEL_NAME,
                )
                await asyncio.to_thread(_save_chat_history, chat_request, msg, original_user_message)
                return JSONResponse(content=response, status_code=200)
        # ────────────────────────────────────────────────────────────────────

        if use_rag and resolved_sigun_filters is None:
            sigun_filters, need_sigun_clarify, sigun_ask_msg = check_sigun(
                user_message, chat_request.messages
            )
            if need_sigun_clarify:
                sigun_ask_count = count_sigun_ask_attempts(chat_request.messages)
                if sigun_ask_count >= MAX_SIGUN_ASK_ATTEMPTS:
                    response = build_chat_response(
                        response_message=MSG_SIGUN_FAILURE,
                        user_message=user_message,
                        model_name=Config.MODEL_NAME,
                    )
                    return JSONResponse(content=response, status_code=200)
                return await _handle_clarify_response(
                    original_user_message,
                    sigun_ask_msg,
                    chat_request,
                    stream,
                )
            resolved_sigun_filters = sigun_filters if sigun_filters else None
            logger.info(f"[SigunCheck/non-stream] sigun_filters={resolved_sigun_filters}")

        # ── [2단계] 통합 전처리 (5개 작업, use_rag 전달) ─────────────────────
        _t = time.monotonic()
        _preprocess_messages = None if skip_clarification_check else chat_request.messages
        preprocess = await unified_preprocess(user_message, _preprocess_messages, use_rag=use_rag)
        logger.info("[TIMING] unified_preprocess(non-stream): %.3fs", time.monotonic() - _t)
        user_message = preprocess["query"]
        await _update_user_message(chat_request.messages, user_message)

        user_intent      = preprocess["intent"]
        reformed_query   = preprocess["reformed_query"]
        expanded_queries = preprocess["expanded_queries"]
        keywords         = preprocess["keywords"]
        # ────────────────────────────────────────────────────────────────────

        # ── 생애주기 체크 (guide_recommend 전용) ─────────────────────────────
        if use_rag and user_intent == "guide_recommend":
            lifecycle, need_lifecycle_clarify, _ = check_lifecycle(
                user_message, chat_request.messages
            )
            if need_lifecycle_clarify:
                logger.info("[LifecycleCheck/non-stream] 생애주기 미확인 → 되묻기 없이 진행")
            else:
                logger.info(f"[LifecycleCheck/non-stream] 생애주기 확인: '{lifecycle}'")
        # ────────────────────────────────────────────────────────────────────

        if not use_rag:
            return await _handle_no_rag_mode(user_message, chat_request, stream)

        return await _handle_rag_mode(
            user_message,
            chat_request,
            chat_request.conv_id,
            stream,
            intent=user_intent,
            sigun_filters=resolved_sigun_filters,
            reformed_query=reformed_query,
            expanded_queries=expanded_queries,
            keywords=keywords,
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

        # TODO: 임시 비활성화
        # recommended_questions(후속 질문 추천) 전달 중단 요청으로 생성/응답을 막는다.
        # questions = await generate_suggested_questions(
        #     user_query=user_query,
        #     assistant_response=assistant_response,
        #     max_questions=5
        # )
        # return JSONResponse(content={"questions": questions}, status_code=200)
        return JSONResponse(content={"questions": []}, status_code=200)

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
