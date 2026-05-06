"""채팅 라우트 핸들러"""

import asyncio
import logging
import time
import uuid
from typing import Any, Dict

from fastapi import APIRouter, Body, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.core.config import Config
from app.shared.schemas import ChatRequest, RecommendStartRequest
from app.core.logging_context import set_log_context, reset_log_context
from app.chat.service import convert_korean_to_standard, generate_suggested_questions
from app.dependencies import get_suggest_questions_service
from app.core.protocols import SuggestQuestionsServiceProtocol
from app.shared.utils import is_clarification_answer
from app.chat.sigun import (
    count_sigun_ask_attempts,
    MSG_SIGUN_FAILURE,
    MSG_OUT_OF_SCOPE_TEMPLATE,
    MAX_SIGUN_ASK_ATTEMPTS,
)
from app.shared.utils import build_chat_response, create_error_detail
from app.dependencies import (
    _save_chat_history,
    _update_user_message,
    _validate_request,
    _validate_user_message,
    chat_user_id_requires_nologin_canonical,
)
from ._stream_utils import _build_streaming_response
from ._conversation_ctx import _check_user_limit, _merge_and_init_conversation
from ._helpers import (
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
    build_preprocess_skip_unified_recommended_question,
)
from ._streaming import _streaming_chat_flow

logger = logging.getLogger(__name__)

router = APIRouter()


def _ensure_runtime_user_id(chat_request: ChatRequest) -> None:
    """비로그인·임시 user_id 를 conv_id 로 고정해 로그인과 같은 히스토리·전처리 경로를 쓴다."""
    if not chat_user_id_requires_nologin_canonical(chat_request.user_id):
        return

    if not chat_request.conv_id:
        chat_request.conv_id = str(uuid.uuid4())

    chat_request.user_id = str(chat_request.conv_id)


async def _chat_completions_core(request: Request, *, llm_recommended_followup: bool) -> Any:
    """공통 채팅 처리. llm_recommended_followup=True(/v1/chat/recommended-question)이면 body mode·의도분석 없이 전용 RAG만 수행."""
    log_context_tokens = None
    t_request_start = time.monotonic()
    origin = request.headers.get("origin")
    ep = "recommended-question" if llm_recommended_followup else "completions"
    logger.info("[Chat %s] origin=%s", ep, origin)
    try:
        data: Dict[str, Any] = await request.json()

        is_valid, error_response = _validate_request(data)
        if not is_valid:
            return error_response

        chat_request = ChatRequest(**data)
        _ensure_runtime_user_id(chat_request)
        log_context_tokens = set_log_context(chat_request.conv_id, chat_request.user_id)
        first_msg_preview = chat_request.messages[0].get('content', '')[:50] if chat_request.messages else 'None'
        logger.info(
            "[Chat Request] endpoint=%s user_id=%s conv_id=%s stream=%s mode=%s",
            ep,
            chat_request.user_id,
            chat_request.conv_id,
            data.get("stream"),
            getattr(chat_request, "mode", ""),
        )
        logger.debug("[Chat Request] first_message_preview=%s", first_msg_preview)
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

        if llm_recommended_followup:
            if Config.KOREAN_STANDARDIZATION_ENABLED:
                converted = await convert_korean_to_standard(user_message)
                if converted and isinstance(converted, str):
                    await _update_user_message(chat_request.messages, converted)
            return await _handle_rag_mode(
                user_message,
                chat_request,
                chat_request.conv_id,
                stream,
                intent="guide_recommend",
                llm_recommended_followup=True,
            )

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
                intent="guide_recommend",
                llm_recommended_followup=False,
            )

        if stream:
            return StreamingResponse(
                _streaming_chat_flow(
                    chat_request,
                    user_message,
                    original_user_message,
                    is_clarification,
                    t_request_start=t_request_start,
                    llm_recommended_followup=llm_recommended_followup,
                ),
                media_type="text/event-stream",
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
        user_message, _ = await _run_query_recreation(user_message, chat_request, is_clarification)

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

        # [6] 통합 전처리 (추천 후속 전용 API는 LLM 생략)
        if llm_recommended_followup:
            preprocess = build_preprocess_skip_unified_recommended_question(user_message)
            logger.info("[ChatFlow] recommended-question API → unified_preprocess LLM 생략 (non-stream)")
        else:
            # 짧은 후속·되묻기 재구성 직후에도 항상 전체 메시지를 넘김 → 스레드 길이(로그인/비로그인)와 무관하게 동일 형식 입력
            preprocess = await run_unified_preprocess(
                user_message, chat_request.messages, use_rag
            )
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
            llm_recommended_followup=llm_recommended_followup,
            search_target=getattr(preprocess, "search_target", None),
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


@router.post('/v1/chat/completions')
async def chat_completions(request: Request):
    """Chat Completions API endpoint."""
    return await _chat_completions_core(request, llm_recommended_followup=False)


@router.post('/v1/chat/recommended-question')
async def chat_recommended_question(request: Request):
    """추천 후속 질문 전용: metadata.referenced_documents 기준 Mariner 검색 후 본문과 classification_llm_recommended_prompt로 LLM 응답."""
    return await _chat_completions_core(request, llm_recommended_followup=True)


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
async def suggest_questions(
    request: Request,
    service: SuggestQuestionsServiceProtocol = Depends(get_suggest_questions_service),
):
    """
    추천 후속 질문 생성.

    - conv_id: DB 히스토리를 불러 마지막 user/assistant와 referenced_documents 발췌를 프롬프트에 넣음 (권장).
    - conv_id 없음: body의 user_query + assistant_response만 사용 (레거시).

    service 의존성은 dependency_overrides로 Mock 교체 가능:
        app.dependency_overrides[get_suggest_questions_service] = lambda: MockService()
    """
    try:
        body = await request.json()
        conv_id = str(body.get("conv_id") or "").strip()
        user_query = body.get("user_query", "") or ""
        assistant_response = body.get("assistant_response", "") or ""
        is_clarification = body.get("is_clarification", False)

        messages = None
        if conv_id:
            from app.conversation.history import get_chat_history_service
            from app.dependencies import _resolve_chat_history_user_id

            raw_uid = body.get("user_id")
            scope_uid = raw_uid.strip() if isinstance(raw_uid, str) and raw_uid.strip() else None
            history_service = get_chat_history_service(Config)
            persist = _resolve_chat_history_user_id(scope_uid, conv_id)
            messages = history_service.get_history(conv_id, persist) or []
            if not messages:
                return JSONResponse(
                    content={
                        "detail": [create_error_detail(
                            loc=["body", "conv_id"],
                            msg="해당 conv_id의 대화 이력이 없습니다",
                            type_="value_error",
                        )]
                    },
                    status_code=404,
                )

        if not conv_id and (not str(user_query).strip() or not str(assistant_response).strip()):
            return JSONResponse(
                content={
                    "detail": [create_error_detail(
                        loc=["body"],
                        msg="conv_id가 있으면 서버가 히스토리를 사용합니다. 없으면 user_query와 assistant_response가 필요합니다",
                        type_="value_error",
                    )]
                },
                status_code=400,
            )

        if is_clarification:
            return JSONResponse(content={"questions": []}, status_code=200)

        questions = await service.generate(
            max_questions=5,
            messages=messages,
            user_query=str(user_query),
            assistant_response=str(assistant_response),
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
