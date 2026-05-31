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
from ._stream_utils import _build_streaming_response, SSE_RESPONSE_HEADERS
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
    run_topic_clarify_check,
    run_out_of_scope_check,
    run_sigun_check,
    run_unified_preprocess,
    run_lifecycle_check,
    build_preprocess_skip_unified_recommended_question,
)
from app.shared.utils import get_second_last_user_message
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

        # _validate_request: 요청 body 스키마/필수 필드 검증, 실패 시 400 응답 객체 반환
        is_valid, error_response = _validate_request(data)
        if not is_valid:
            return error_response

        raw_user_id = data.get("user_id")
        raw_conv_id = data.get("conv_id")
        logger.info(
            "[Chat Request Raw] endpoint=%s user_id=%s conv_id=%s stream=%s mode=%s",
            ep,
            raw_user_id,
            raw_conv_id,
            data.get("stream"),
            data.get("mode", ""),
        )

        # ChatRequest: pydantic 모델로 dict → 타입 검증된 객체로 변환
        chat_request = ChatRequest(**data)
        # _ensure_runtime_user_id: 비로그인/임시 user_id를 conv_id로 고정해 히스토리 경로를 로그인과 동일하게 통일
        _ensure_runtime_user_id(chat_request)
        if raw_conv_id != chat_request.conv_id or raw_user_id != chat_request.user_id:
            logger.info(
                "[Chat Request Normalized] endpoint=%s raw_user_id=%s raw_conv_id=%s -> user_id=%s conv_id=%s",
                ep,
                raw_user_id,
                raw_conv_id,
                chat_request.user_id,
                chat_request.conv_id,
            )
        # set_log_context: 이 요청의 모든 로그에 conv_id/user_id 태그를 자동 부착 (contextvar, finally에서 해제)
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
        before_merge_conv_id = chat_request.conv_id
        before_merge_user_id = chat_request.user_id
        # _merge_and_init_conversation: 동일 conv_id의 DB 히스토리를 messages에 머지하고 새 conv 레코드 초기화
        await _merge_and_init_conversation(chat_request)
        if before_merge_conv_id != chat_request.conv_id or before_merge_user_id != chat_request.user_id:
            logger.info(
                "[Chat Request Merge Normalized] endpoint=%s user_id=%s conv_id=%s -> user_id=%s conv_id=%s",
                ep,
                before_merge_user_id,
                before_merge_conv_id,
                chat_request.user_id,
                chat_request.conv_id,
            )

        # _validate_user_message: messages에서 마지막 user 메시지 추출 + 빈값/길이 검증
        is_valid, error_response, user_message = _validate_user_message(chat_request.messages)
        if not is_valid:
            return error_response

        original_user_message = user_message
        stream = data.get("stream", False)
        # is_clarification_answer: 직전 assistant가 되묻기였는지 감지
        is_clarification = is_clarification_answer(chat_request.messages)

        # step6: 후속 의도분류(classify_next_intent/more_results) 제거 — 모든 턴 재작성→분류 단일 경로.
        # "더 알려줘"는 전량 노출로 폐지, 더보기는 프론트 칩 드릴다운(후속 질의)으로 대체.

        if not is_clarification:
            # _check_user_limit: 동시 사용자 한도(MAX_CONCURRENT_USERS=50, 세션 10분) 초과 시 안내 응답 반환
            limit_response = _check_user_limit(user_message, chat_request, stream)
            if limit_response:
                return limit_response

        # B2: 구조화 칩 드릴다운 — 직전 추천 슬롯 + 선택 주제를 그대로 받아 pre_check/재작성/분류
        # 전부 건너뛰고 guide_recommend DB 조회로 직행(결정적, LLM 0회). NL 후속 재작성 실패와 무관.
        dd = getattr(chat_request, "drilldown", None)
        if dd:
            _dd_sigun = str(dd.get("sigun") or "").strip()
            _raw_cat = dd.get("topic_category")
            _dd_cat_list = _raw_cat if isinstance(_raw_cat, list) else ([_raw_cat] if _raw_cat else [])
            _dd_topic_cat = [str(t).strip() for t in _dd_cat_list if str(t).strip()]
            _dd_topic_kw = [str(t).strip() for t in (dd.get("topic_keyword") or []) if str(t).strip()]
            _dd_label = (_dd_topic_kw[:1] or _dd_topic_cat[:1] or ["복지"])[0]
            logger.info(
                "[Drilldown] conv_id=%s | sigun=%s lifecycle=%s household=%s topic_cat=%s topic_kw=%s → DB 직행",
                chat_request.conv_id, _dd_sigun, dd.get("lifecycle"), dd.get("household"),
                _dd_topic_cat, _dd_topic_kw,
            )
            return await _handle_rag_mode(
                _dd_label,
                chat_request,
                chat_request.conv_id,
                stream,
                intent="guide_recommend",
                sigun_filters=[_dd_sigun] if _dd_sigun else None,
                reformed_query=_dd_label,
                lifecycle_tags=[str(t).strip() for t in (dd.get("lifecycle") or []) if str(t).strip()],
                household_tags=[str(t).strip() for t in (dd.get("household") or []) if str(t).strip()],
                topic_category=_dd_topic_cat,
                topic_keyword=_dd_topic_kw,
                must_not_keywords=[str(t).strip() for t in (dd.get("must_not") or []) if str(t).strip()],
            )

        if llm_recommended_followup:
            if Config.KOREAN_STANDARDIZATION_ENABLED:
                # convert_korean_to_standard: 방언/구어체를 표준어로 정규화 (LLM 1회 호출)
                converted = await convert_korean_to_standard(user_message)
                if converted and isinstance(converted, str):
                    await _update_user_message(chat_request.messages, converted)
            # _handle_rag_mode: 의도별 pipeline_*.py로 라우팅해 RAG 검색 + LLM 응답 생성 (스트리밍/논스트리밍 둘 다 처리)
            return await _handle_rag_mode(
                user_message,
                chat_request,
                chat_request.conv_id,
                stream,
                intent="guide_recommend",
                llm_recommended_followup=True,
                reformed_query=None,
                excluded_chunk_ids=[],
                excluded_service_names=[],
                final_user_message=None,
                more_info=False,
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
                reformed_query=None,
                excluded_chunk_ids=[],
                excluded_service_names=[],
                final_user_message=None,
                more_info=False,
            )

        if stream:
            # _streaming_chat_flow: SSE 스트리밍 본 흐름 — 8단계를 비동기 제너레이터로 흘리면서 상태/청크 yield
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
                headers=SSE_RESPONSE_HEADERS,
            )

        # Non-streaming flow
        # [1] PreCheck: RAG 여부 + 되묻기
        # run_pre_check: RAG 사용 여부 판단(use_rag) + 정보 부족 시 되묻기 질문 생성
        pre = await run_pre_check(user_message, chat_request.messages, is_clarification)
        use_rag = pre.use_rag
        if pre.clarification_question:
            # _save_chat_history: 되묻기 질문을 DB에 영속화 (동기 함수라 to_thread로 비동기 래핑)
            await asyncio.to_thread(_save_chat_history, chat_request, pre.clarification_question, original_user_message)
            # _handle_clarify_response: 되묻기 응답을 chat completion 포맷(또는 SSE)으로 감싸 반환
            return await _handle_clarify_response(original_user_message, pre.clarification_question, chat_request, stream)

        # [2] 되묻기 답변의 시군 조기 확인 (query_recreation LLM 호출 전)
        resolved_sigun_filters = None
        # run_early_sigun_check: 되묻기 답변일 때만 동작 — 쿼리 재구성 LLM이 시군을 오염시키기 전에 시군 확정
        early_sigun = run_early_sigun_check(user_message, chat_request.messages, use_rag, is_clarification)
        if early_sigun is not None:
            if early_sigun.need_clarify:
                # count_sigun_ask_attempts: 같은 대화에서 시군 되묻기 누적 횟수 카운트 (3회 초과 시 안내 종료)
                if count_sigun_ask_attempts(chat_request.messages) >= MAX_SIGUN_ASK_ATTEMPTS:
                    # build_chat_response: OpenAI Chat Completions 스키마에 맞춰 응답 dict 구성
                    return JSONResponse(content=build_chat_response(response_message=MSG_SIGUN_FAILURE, user_message=user_message, model_name=Config.MODEL_NAME), status_code=200)
                return await _handle_clarify_response(original_user_message, early_sigun.ask_message, chat_request, stream)
            resolved_sigun_filters = early_sigun.filters
            logger.info("[SigunCheck/non-stream] 조기 확정(is_clarification): %s", resolved_sigun_filters)

        # [2.5] 분야 되묻기: 원질문이 정보량 0("알려줘" 등)이면 query_recreation 전 1회 되묻기.
        _original_q_for_topic = get_second_last_user_message(chat_request.messages) or original_user_message
        topic_clarify = run_topic_clarify_check(
            _original_q_for_topic,
            chat_request.messages,
            use_rag,
            is_clarification,
            current_user_message=user_message,
        )
        if topic_clarify is not None and topic_clarify.need_clarify:
            logger.info("[TopicClarify/non-stream] 정보량 0 원질문 감지 → 분야 되묻기")
            await asyncio.to_thread(_save_chat_history, chat_request, topic_clarify.ask_message, original_user_message)
            return await _handle_clarify_response(original_user_message, topic_clarify.ask_message, chat_request, stream)

        # [3] 쿼리 재구성 — 멀티턴 문맥을 반영해 단일 검색 쿼리로 재작성 (step6: 모든 턴 동일 경로)
        user_message, _ = await _run_query_recreation(user_message, chat_request, is_clarification)

        # [4] 경상남도 외 지역 체크
        # run_out_of_scope_check: 쿼리에 타 시도(서울/부산 등)가 명시되면 안내 메시지로 조기 종료
        out_of_scope, region_name = run_out_of_scope_check(user_message, use_rag)
        if out_of_scope:
            msg = MSG_OUT_OF_SCOPE_TEMPLATE.format(region=region_name)
            await asyncio.to_thread(_save_chat_history, chat_request, msg, original_user_message)
            return JSONResponse(content=build_chat_response(response_message=msg, user_message=user_message, model_name=Config.MODEL_NAME), status_code=200)

        # [5] 시군 체크
        # run_sigun_check: 경남 18개 시군 중 어느 곳을 묻는지 추출, 모호하면 되묻기 ask_message 생성 (조기 확정 못한 케이스 대상)
        sigun = run_sigun_check(user_message, chat_request.messages, use_rag, resolved_sigun_filters)
        if sigun is not None:
            if sigun.need_clarify:
                if count_sigun_ask_attempts(chat_request.messages) >= MAX_SIGUN_ASK_ATTEMPTS:
                    return JSONResponse(content=build_chat_response(response_message=MSG_SIGUN_FAILURE, user_message=user_message, model_name=Config.MODEL_NAME), status_code=200)
                return await _handle_clarify_response(original_user_message, sigun.ask_message, chat_request, stream)
            resolved_sigun_filters = sigun.filters
            logger.info("[SigunCheck/non-stream] sigun_filters=%s", resolved_sigun_filters)

        # [6] 통합 전처리 (추천 후속 전용 API는 LLM 생략)
        # 배제 사업명(LLM 추출)은 분기에 따라 unified_preprocess와 병렬로 호출.
        # 히스토리 재사용 분기에서는 LLM 호출 없이 빈 리스트 유지.
        llm_excluded_services: list = []
        if llm_recommended_followup:
            # build_preprocess_skip_unified_recommended_question: 통합 전처리 LLM 호출을 생략하고 PreprocessResult를 즉시 구성
            preprocess = build_preprocess_skip_unified_recommended_question(user_message)
            logger.info("[ChatFlow] recommended-question API → unified_preprocess LLM 생략 (non-stream)")
        else:
            # 짧은 후속·되묻기 재구성 직후에도 항상 전체 메시지를 넘김 → 스레드 길이(로그인/비로그인)와 무관하게 동일 형식 입력
            # unified_preprocess가 must_not_keywords/exclusion_intent를 함께 산출 → 별도 extract 호출 생략 (32B 1회 절약).
            preprocess = await run_unified_preprocess(
                user_message, chat_request.messages, use_rag
            )
            llm_excluded_services = list(preprocess.must_not_keywords or [])

        user_message = preprocess.query
        await _update_user_message(chat_request.messages, user_message)

        # [7] 생애주기 체크 (guide_recommend 전용, 로그만)
        # run_lifecycle_check: 생애주기 키워드(영유아/청년/노인 등) 매칭 로그만 출력, 분기에는 영향 없음
        run_lifecycle_check(user_message, chat_request.messages, use_rag, preprocess.intent)

        if not use_rag:
            # _handle_no_rag_mode: 인사·잡담 등 검색 불필요한 입력에 대해 LLM 직답 응답 생성
            return await _handle_no_rag_mode(user_message, chat_request)

        # _handle_rag_mode: preprocess.intent에 따라 pipeline_general/comparison/guide_recommend/search 중 하나로 라우팅
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
            lifecycle_tags=getattr(preprocess, "lifecycle_tags", None),
            household_tags=getattr(preprocess, "household_tags", None),
            topic_category=getattr(preprocess, "topic_category", None),
            topic_keyword=getattr(preprocess, "topic_keyword", None),
            must_not_keywords=getattr(preprocess, "must_not_keywords", None),
            excluded_chunk_ids=[],
            excluded_service_names=[],
            llm_excluded_services=llm_excluded_services,
            final_user_message=None,
            more_info=False,
            more_detail=getattr(preprocess, "detail_requested", False),
        )

    except Exception as e:
        logger.error(f"Chat completions error: {e}", exc_info=True)
        return JSONResponse(
            content={
                # create_error_detail: FastAPI 표준 에러 detail dict 구성 (loc/msg/type 필드)
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
            # reset_log_context: set_log_context로 박은 contextvar 토큰을 해제 (다음 요청에 누수 방지)
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
                headers=SSE_RESPONSE_HEADERS,
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
