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
from app.chat.more_results import (
    get_last_preprocess_from_history,
    get_base_user_query_from_history,
    get_excluded_info_from_history,
    collect_prior_service_names,
)
from app.chat.routing import classify_next_intent
from app.chat.intent_registry import resolve_reused_intent_on_more
from app.shared.utils.keyword_extractor import extract_nouns
from ._stream_utils import _build_streaming_response, SSE_RESPONSE_HEADERS
from ._conversation_ctx import _check_user_limit, _merge_and_init_conversation
from ._helpers import (
    _handle_clarify_response,
    _handle_no_rag_mode,
    _handle_rag_mode,
    _run_query_recreation,
)
from ._pipeline_steps import (
    PreprocessResult,
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

_MORE_INFO_REUSABLE_INTENTS = {"guide_recommend", "search", "comparison"}


def _is_context_dependent_followup(text: str) -> bool:
    """명시 주제가 부족해 직전 문맥 의존 가능성이 큰 짧은 후속 발화인지 판정."""
    q = str(text or "").strip()
    if not q:
        return False
    compact = "".join(q.split())
    if len(compact) > 20:
        return False
    try:
        nouns = [n.strip() for n in extract_nouns(q, use_bigram=False) if n and n.strip()]
    except Exception:
        nouns = []
    return len(nouns) <= 1


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
        # non-stream / 추천 API / guide_recommend 모드일 때만 "더 알려줘" 후속 의도 분석 경로를 탐 (스트리밍은 _streaming_chat_flow에서 별도 처리)
        use_more_results_router_path = (not stream) or llm_recommended_followup or (chat_request.mode == "guide_recommend")

        # is_clarification_answer: 직전 assistant가 되묻기였는지 감지
        is_clarification = is_clarification_answer(chat_request.messages)

        more_detected = False
        more_blocked_followup = False
        more_topic_switch = False
        more_final_user_message = None
        more_excluded_chunk_ids = []
        more_excluded_service_names = []
        more_last_preprocess = None

        if use_more_results_router_path:
            # get_last_preprocess_from_history: 직전 assistant 메타에서 preprocess(intent/reformed_query/expanded_queries 등) 복원
            more_last_preprocess = get_last_preprocess_from_history(chat_request.messages)
            # 같은 함수의 필터 버전: 재사용 가능한 intent(guide_recommend/search/comparison)만 허용
            more_reusable_preprocess = get_last_preprocess_from_history(
                chat_request.messages,
                allowed_intents=_MORE_INFO_REUSABLE_INTENTS,
            )
            # get_base_user_query_from_history: 멀티턴의 '원래 질문'(base) 추출 → MORE_INFO 시 재사용
            base_user_query = get_base_user_query_from_history(chat_request.messages)
            # _is_context_dependent_followup: 너무 짧고 명사 1개 이하면 단독 검색이 무의미한 후속 발화로 간주
            if base_user_query and _is_context_dependent_followup(base_user_query):
                base_user_query = ""
            prior_intent_value = str((more_last_preprocess or {}).get("intent") or "")
            # collect_prior_service_names: 직전까지 노출된 서비스명 목록 (MORE_DETAIL 분기 보조용)
            prior_service_names = collect_prior_service_names(chat_request.messages)
            # classify_next_intent: 후속 의도 분류 LLM (MORE_INFO/MORE_DETAIL/NEW_SEARCH/REFINE_SEARCH/CLARIFY_REPLY)
            next_intent = await classify_next_intent(
                chat_request.messages,
                user_message,
                prior_intent=prior_intent_value,
                prior_service_names=prior_service_names,
                is_clarification_question=is_clarification,
            )
            intent_label = next_intent.get("intent")
            llm_detected_more = intent_label in ("MORE_INFO", "MORE_DETAIL")
            llm_detected_more_detail = intent_label == "MORE_DETAIL"
            more_topic_switch = intent_label in ("NEW_SEARCH", "REFINE_SEARCH")
            # CLARIFY_REPLY는 query_recreation 흐름에서 원질문과 합성하므로 더알려줘 분기 비활성.
            more_detected = llm_detected_more
            # 직전 preprocess 메타가 없을 때만 history 복구를 시도한다.
            # 직전 intent가 general이어도 more_last_preprocess가 있으면 해당 intent 축을 그대로 재사용한다.
            if more_detected and not more_last_preprocess:
                can_recover_from_history = bool(
                    more_reusable_preprocess
                    and (llm_detected_more or _is_context_dependent_followup(user_message))
                )
                if can_recover_from_history:
                    more_last_preprocess = more_reusable_preprocess
                    logger.info(
                        "[MoreResults/non-stream] 직전 general/메타누락 감지 → 재사용 가능한 직전 intent로 복구 intent=%s",
                        more_last_preprocess.get("intent"),
                    )
                elif base_user_query:
                    if llm_detected_more_detail:
                        logger.info(
                            "[MoreResults/non-stream] 직전 메타누락 + base_query 존재 + MORE_DETAIL → 상세 질의 유지(next_intent re_query 사용)",
                        )
                    else:
                        logger.info(
                            "[MoreResults/non-stream] 직전 메타누락 + base_query 존재 → MORE_INFO 유지(base_query 재사용)",
                        )
                else:
                    more_detected = False
                    more_blocked_followup = True
                    logger.debug("[MoreResults/non-stream] 직전 intent=general 또는 메타 없음 → MORE_INFO 비활성화")
            if more_detected:
                re_query = None
                if llm_detected_more_detail:
                    re_query = str(next_intent.get("re_query", "") or "").strip()
                    if not re_query:
                        re_query = (original_user_message or user_message or "").strip()
                elif (
                    more_last_preprocess
                    and more_last_preprocess.get("reformed_query")
                ):
                    re_query = str(more_last_preprocess["reformed_query"]).strip()
                elif base_user_query:
                    re_query = str(base_user_query).strip()
                else:
                    fallback_rq = str(next_intent.get("re_query", "") or "").strip()
                    if fallback_rq:
                        re_query = fallback_rq

                if re_query:
                    user_message = re_query
                    # _update_user_message: messages 리스트의 마지막 user 메시지 content를 새 문자열로 갱신
                    await _update_user_message(chat_request.messages, user_message)

                # get_excluded_info_from_history: 직전까지 노출한 chunk_id / service_name을 모아 검색 제외 목록 구성
                (
                    more_excluded_chunk_ids,
                    more_excluded_service_names,
                ) = get_excluded_info_from_history(chat_request.messages)
                if llm_detected_more_detail:
                    # MORE_DETAIL(general 세부 요청)은 제외 로직 적용 안 함
                    more_excluded_chunk_ids, more_excluded_service_names = [], []
                    logger.info("[MoreResults/non-stream] MORE_DETAIL → 검색 제외 목록 초기화")

                llm_rq = str((next_intent or {}).get("llm_re_query", "") or "").strip()
                more_final_user_message = llm_rq or (original_user_message or "").strip() or None

        if not is_clarification:
            # _check_user_limit: 동시 사용자 한도(MAX_CONCURRENT_USERS=50, 세션 10분) 초과 시 안내 응답 반환
            limit_response = _check_user_limit(user_message, chat_request, stream)
            if limit_response:
                return limit_response

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
                reformed_query=user_message if more_detected else None,
                excluded_chunk_ids=more_excluded_chunk_ids,
                excluded_service_names=more_excluded_service_names,
                final_user_message=more_final_user_message,
                more_info=more_detected,
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
                reformed_query=user_message if more_detected else None,
                excluded_chunk_ids=more_excluded_chunk_ids,
                excluded_service_names=more_excluded_service_names,
                final_user_message=more_final_user_message,
                more_info=more_detected,
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
        # MORE_INFO/주제전환 분기는 직전 검색 컨텍스트를 재사용하므로 생략.
        if not more_detected and not more_topic_switch:
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

        # [3] 쿼리 재구성 (MORE_INFO 또는 general 후속 경로는 reformed_query를 직접 사용하므로 생략)
        # NEW_SEARCH/REFINE_SEARCH(주제 전환)도 스킵 — 직전 주제로 오염 방지
        if not more_detected and not more_blocked_followup and not more_topic_switch:
            # _run_query_recreation: 멀티턴 문맥을 반영해 user_message를 단일 검색 가능 쿼리로 재작성 (LLM 호출)
            user_message, _ = await _run_query_recreation(user_message, chat_request, is_clarification)
        elif more_topic_switch:
            logger.info(
                "[Query Recreation/non-stream] conv_id=%s | next_intent=NEW_SEARCH/REFINE_SEARCH → 재구성 스킵",
                chat_request.conv_id,
            )

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
        if more_detected and more_last_preprocess:
            previous_intent = str(more_last_preprocess.get("intent") or "general")
            # resolve_reused_intent_on_more: MORE_INFO/DETAIL일 때 재사용할 intent 결정 (MORE_INFO는 guide_recommend 강제 등)
            reused_intent = resolve_reused_intent_on_more(
                previous_intent,
                more_detail=llm_detected_more_detail,
                user_message=user_message,
            )
            try:
                # extract_nouns: kiwi 형태소 분석기로 명사 추출 (Mariner 키워드 검색용)
                reused_keywords = extract_nouns(user_message)
            except Exception:
                reused_keywords = []
            # PreprocessResult: 전처리 결과 dataclass — query/intent/reformed_query/expanded_queries/keywords 등을 묶음
            preprocess = PreprocessResult(
                query=user_message,
                intent=reused_intent,
                intent_reason=(
                    "reused_from_history_on_more_detail"
                    if llm_detected_more_detail
                    else (
                        "forced_guide_recommend_on_more_info"
                        if reused_intent == "guide_recommend"
                        else "reused_from_history_on_more_info"
                    )
                ),
                reformed_query=user_message,
                expanded_queries=(
                    [user_message]
                    if llm_detected_more_detail
                    else more_last_preprocess.get("expanded_queries") or [user_message]
                ),
                keywords=reused_keywords,
                elapsed=0.0,
                search_target=more_last_preprocess.get("search_target"),
            )
            logger.info(
                "[MoreResults/non-stream] 히스토리 전처리 재사용 intent=%s (llm_more_detail=%s)",
                preprocess.intent,
                llm_detected_more_detail,
            )
        elif more_blocked_followup and more_last_preprocess:
            # 직전 general 후속 — exclusion 없이 직전 reformed_query + intent 재사용
            re_query = str(more_last_preprocess.get("reformed_query") or "").strip() or user_message
            user_message = re_query
            await _update_user_message(chat_request.messages, user_message)
            try:
                reused_keywords = extract_nouns(user_message)
            except Exception:
                reused_keywords = []
            preprocess = PreprocessResult(
                query=user_message,
                intent=str(more_last_preprocess.get("intent") or "general"),
                intent_reason="reused_from_history_general_followup",
                reformed_query=user_message,
                expanded_queries=more_last_preprocess.get("expanded_queries") or [user_message],
                keywords=reused_keywords,
                elapsed=0.0,
                search_target=more_last_preprocess.get("search_target"),
            )
            logger.info("[MoreResults/non-stream] general 후속 → 히스토리 intent 재사용 intent=%s reformed=%s (exclusion 없음)", preprocess.intent, user_message[:60])
        elif llm_recommended_followup:
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

        # MORE_INFO는 직전 intent와 무관하게 guide_recommend로 강제한다.
        # (MORE_DETAIL은 기존 축 유지)
        if more_detected and not llm_detected_more_detail and preprocess.intent != "guide_recommend":
            prev_intent = preprocess.intent
            preprocess.intent = "guide_recommend"
            prev_reason = (preprocess.intent_reason or "").strip()
            suffix = "forced_guide_recommend_on_more_info(no_history_fallback)"
            preprocess.intent_reason = f"{prev_reason} | {suffix}" if prev_reason else suffix
            logger.info(
                "[MoreResults/non-stream] MORE_INFO 강제 intent=guide_recommend (preprocess_intent_was=%s)",
                prev_intent,
            )

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
            policy_priority_tag=getattr(preprocess, "policy_priority_tag", None),
            lifecycle_tags=getattr(preprocess, "lifecycle_tags", None),
            household_tags=getattr(preprocess, "household_tags", None),
            excluded_chunk_ids=more_excluded_chunk_ids,
            excluded_service_names=more_excluded_service_names,
            llm_excluded_services=llm_excluded_services,
            final_user_message=more_final_user_message,
            more_info=more_detected,
            more_detail=llm_detected_more_detail or getattr(preprocess, "detail_requested", False),
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
