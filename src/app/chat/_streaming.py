"""스트리밍 채팅 응답 흐름"""

import asyncio
import csv
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncGenerator, List, Optional

from app.core.config import Config
from app.shared.schemas import ChatRequest
from app.core.logging_context import set_log_context, reset_log_context
from app.chat.service import call_llm_api
from app.chat.sigun import (
    count_sigun_ask_attempts,
    MSG_SIGUN_FAILURE,
    MSG_OUT_OF_SCOPE_TEMPLATE,
    MAX_SIGUN_ASK_ATTEMPTS,
)
from app.shared.utils import build_chat_response, load_system_prompt, shorten_text
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
from app.shared.utils.status_messages import (
    build_status_message,
    STATUS_QUERY_RECREATION,
)
from app.dependencies import (
    _build_llm_kwargs,
    _save_chat_history,
    _update_user_message,
)
from ._doc_filter import _filter_referenced_documents_by_response
from ._stream_utils import (
    _build_streaming_response,
    _stream_delta_content,
    drain_status_until_done,
)
from ._helpers import _get_rag_processor, _run_query_recreation
from ._conversation_ctx import _save_stream_history
from app.chat.more_results import (
    get_excluded_info_from_history,
    get_base_user_query_from_history,
    get_last_preprocess_from_history,
    collect_prior_service_names,
)
from app.shared.utils.keyword_extractor import extract_nouns
from app.chat.infra.rag import filter_okms_keywords
from app.chat.routing import classify_next_intent
from app.chat.intent_registry import (
    lookup as _lookup_intent_spec,
    resolve_reused_intent_on_more,
)

logger = logging.getLogger(__name__)

_MORE_INFO_REUSABLE_INTENTS = {"guide_recommend", "search", "comparison"}

_TIMING_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "tests"))
_TIMING_FIELDS = [
    "timestamp", "user_query",
    # PreCheck 결과
    "use_rag", "clarification_question",
    # UnifiedPreprocess 결과
    "query", "intent", "intent_reason", "reformed_query", "expanded_queries",
    # 키워드 검색어 (벡터 검색어 → kiwi 추출)
    "kw_1", "kw_2", "kw_3", "kw_4", "kw_5",
    # LLM 최종 답변
    "assistant_response",
    # 구간 시간
    "t_pre_check", "t_query_recreation", "t_unified_preprocess",
    "t_lifecycle_check", "t_rag_total", "t_total_flow",
    # TTFT
    "t_ttft",
]


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


def _get_timing_csv_path() -> str:
    date_str = datetime.now().strftime("%Y%m%d")
    return os.path.join(_TIMING_DIR, f"log_{date_str}.csv")


def _write_timing_csv(row: dict) -> None:
    path = _get_timing_csv_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_header = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=_TIMING_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        writer.writerow(row)


@dataclass
class _MoreResultsContext:
    detected: bool = False
    blocked_followup: bool = False  # MORE_INFO였으나 차단됨(직전 general/메타없음) — exclusion 없이 intent만 재사용
    re_query: Optional[str] = None
    final_user_message: Optional[str] = None
    excluded_chunk_ids: List[str] = field(default_factory=list)
    excluded_service_names: List[str] = field(default_factory=list)
    last_preprocess: Optional[dict] = None
    more_detail: bool = False  # general intent일 때 "더 자세" 키워드 감지
    topic_switch: bool = False  # next_intent=NEW_SEARCH/REFINE_SEARCH — query_recreation 스킵용


async def _resolve_more_results_context(
    conv_id: str,
    messages: list,
    user_message: str,
    original_user_message: str,
    *,
    is_clarification_question: bool = False,
) -> _MoreResultsContext:
    """'더 알려줘' 의도를 감지하고 재검색에 필요한 컨텍스트를 반환."""
    last_preprocess = get_last_preprocess_from_history(messages)
    reusable_preprocess = get_last_preprocess_from_history(
        messages,
        allowed_intents=_MORE_INFO_REUSABLE_INTENTS,
    )
    base_user_query = get_base_user_query_from_history(messages)

    # Debug: 출력해 직전 assistant 메시지와 preprocess 후보 확인
    try:
        tail = (messages or [])[-8:]
        logger.debug("[MoreResults debug] messages tail (len=%d): %s", len(messages or []), shorten_text(str(tail), 1000))
        logger.debug("[MoreResults debug] last_preprocess raw: %s", shorten_text(str(last_preprocess), 500))
        logger.debug("[MoreResults debug] reusable_preprocess raw: %s", shorten_text(str(reusable_preprocess), 500))
        logger.debug("[MoreResults debug] base_user_query: %s", shorten_text(str(base_user_query or ""), 300))
    except Exception:
        logger.exception("[MoreResults debug] 로그 생성 중 오류 발생")
    if base_user_query and _is_context_dependent_followup(base_user_query):
        base_user_query = ""
    prior_intent_value = str((last_preprocess or {}).get("intent") or "")
    prior_service_names = collect_prior_service_names(messages)
    next_intent = await classify_next_intent(
        messages,
        user_message,
        prior_intent=prior_intent_value,
        prior_service_names=prior_service_names,
        is_clarification_question=is_clarification_question,
    )
    intent_label = next_intent.get("intent")
    llm_detected_more = intent_label in ("MORE_INFO", "MORE_DETAIL")
    llm_detected_more_detail = intent_label == "MORE_DETAIL"
    topic_switch = intent_label in ("NEW_SEARCH", "REFINE_SEARCH")
    # CLARIFY_REPLY: 되묻기 응답이므로 query_recreation으로 원질문과 합성. 더알려줘 분기는 비활성.
    detected = llm_detected_more

    # 직전 prep:rocess 메타가 없을 때만 history 복구를 시도한다.
    # 직전 intent가 general이어도 last_preprocess가 있으면 해당 intent 축을 그대로 재사용한다.
    if detected and not last_preprocess:
        can_recover_from_history = bool(
            reusable_preprocess
            and (llm_detected_more or _is_context_dependent_followup(user_message))
        )
        if can_recover_from_history:
            last_preprocess = reusable_preprocess
            logger.info(
                "[MoreResults] conv_id=%s | 직전 general/메타누락 감지 → 재사용 가능한 직전 intent로 복구 intent=%s",
                conv_id,
                last_preprocess.get("intent"),
            )
        elif base_user_query:
            if llm_detected_more_detail:
                logger.info(
                    "[MoreResults] conv_id=%s | 직전 메타누락 + base_query 존재 + MORE_DETAIL → 상세 질의 유지(next_intent re_query 사용)",
                    conv_id,
                )
            else:
                logger.info(
                    "[MoreResults] conv_id=%s | 직전 메타누락 + base_query 존재 → MORE_INFO 유지(base_query 재사용)",
                    conv_id,
                )
        else:
            detected = False
            logger.debug(
                "[MoreResults] conv_id=%s | 직전 intent=general 또는 메타 없음 → MORE_INFO 비활성화",
                conv_id,
            )
            # 후속 발화임을 보존: exclusion 없이 직전 intent만 재사용하도록 blocked_followup 설정
            return _MoreResultsContext(
                last_preprocess=last_preprocess,
                blocked_followup=True,
                topic_switch=topic_switch,
            )

    logger.debug(
        "[MoreResults] conv_id=%s | 보조분류 next_intent=%s | re_query=%s",
        conv_id,
        next_intent.get("intent"),
        shorten_text(str(next_intent.get("re_query", "") or ""), 80),
    )

    if not detected:
        return _MoreResultsContext(last_preprocess=last_preprocess, topic_switch=topic_switch)

    re_query: Optional[str] = None
    if llm_detected_more_detail:
        re_query = str(next_intent.get("re_query", "") or "").strip()
        if not re_query:
            re_query = (original_user_message or user_message or "").strip()
        logger.debug("[MoreResults] conv_id=%s | MORE_DETAIL 검색질의 적용: %s", conv_id, shorten_text(re_query, 80))
    elif (
        last_preprocess
        and last_preprocess.get("reformed_query")
    ):
        re_query = str(last_preprocess["reformed_query"]).strip()
        logger.debug("[MoreResults] conv_id=%s | 검색질의 재사용(reformed_query): %s", conv_id, shorten_text(re_query, 80))
    elif base_user_query:
        re_query = base_user_query
        logger.debug("[MoreResults] conv_id=%s | fallback 원질문 재사용: %s", conv_id, shorten_text(re_query, 80))
    else:
        fallback_rq = str(next_intent.get("re_query", "") or "").strip()
        if fallback_rq:
            re_query = fallback_rq
            logger.debug("[MoreResults] conv_id=%s | fallback re_query 적용: %s", conv_id, shorten_text(re_query, 80))

    excluded_chunk_ids, excluded_service_names = get_excluded_info_from_history(messages)
    if llm_detected_more_detail:
        # MORE_DETAIL(general 세부 요청)은 동일 문서를 다시 참조해야 할 수 있으므로 제외 로직 적용 안 함
        excluded_chunk_ids, excluded_service_names = [], []
        logger.info("[MoreResults] conv_id=%s | MORE_DETAIL → 검색 제외 목록 초기화", conv_id)
    else:
        logger.info(
            "[MoreResults] conv_id=%s | 제외 대상 집계 chunk_ids=%d | service_names=%d",
            conv_id, len(excluded_chunk_ids), len(excluded_service_names),
        )
    logger.debug(
        "[MoreResults] conv_id=%s | 제외 샘플 chunk_ids=%s | service_names=%s",
        conv_id, excluded_chunk_ids[:10], excluded_service_names[:10],
    )

    llm_rq = str((next_intent or {}).get("llm_re_query", "") or "").strip()
    final_user_message = llm_rq or (original_user_message or "").strip() or None
    logger.debug(
        "[MoreResults] conv_id=%s | 최종LLM user 문구(next_intent llm_re_query 우선)=%s",
        conv_id, shorten_text(final_user_message or "", 100),
    )

    # more_detail: LLM이 MORE_DETAIL로 분류한 경우 (general intent 직전 대화)
    more_detail = llm_detected_more_detail
    if more_detail:
        logger.info(
            "[MoreResults] conv_id=%s | more_detail 감지 (LLM next_intent=MORE_DETAIL)",
            conv_id,
        )
    
    return _MoreResultsContext(
        detected=True,
        re_query=re_query,
        final_user_message=final_user_message,
        excluded_chunk_ids=excluded_chunk_ids,
        excluded_service_names=excluded_service_names,
        last_preprocess=last_preprocess,
        more_detail=more_detail,
        topic_switch=topic_switch,
    )


def _build_preprocess_from_history(
    user_message: str,
    last_preprocess: dict,
    *,
    override_intent: Optional[str] = None,
    intent_reason: Optional[str] = None,
) -> dict:
    """MORE_INFO 경로에서 히스토리의 전처리 결과를 재사용하여 preprocess_data를 구성."""
    try:
        kw = extract_nouns(user_message)
    except Exception:
        kw = []
    resolved_intent = str(override_intent or last_preprocess.get("intent") or "general")
    return {
        "query": user_message,
        "intent": resolved_intent,
        "intent_reason": (
            intent_reason
            or (
                "forced_guide_recommend_on_more_info"
                if resolved_intent == "guide_recommend"
                else "reused_from_history_on_more_info"
            )
        ),
        "reformed_query": user_message,
        "expanded_queries": last_preprocess.get("expanded_queries") or [user_message],
        "keywords": kw,
        "search_target": last_preprocess.get("search_target"),
    }


def _capture_preprocess_timings(timings: dict, preprocess_data: dict, expanded_queries: list) -> None:
    """전처리 결과를 CSV 타이밍 딕셔너리에 기록."""
    timings["query"]            = preprocess_data.get("query", "")[:200]
    timings["intent"]           = preprocess_data.get("intent", "")
    timings["intent_reason"]    = preprocess_data.get("intent_reason", "")[:200]
    timings["reformed_query"]   = (preprocess_data.get("reformed_query") or "")[:200]
    timings["expanded_queries"] = " | ".join(expanded_queries or [])
    kw_strings = [
        " ".join(filter_okms_keywords(extract_nouns(eq, use_bigram=False)))
        for eq in (expanded_queries or [])
    ]
    for i, kw in enumerate(kw_strings[:5], 1):
        timings[f"kw_{i}"] = kw


async def _streaming_chat_flow(
    chat_request: ChatRequest,
    user_message: str,
    original_user_message: str,
    is_clarification: bool,
    t_request_start: float = 0.0,
    *,
    llm_recommended_followup: bool = False,
) -> AsyncGenerator[str, None]:
    """스트리밍 채팅 응답 전체 흐름을 처리하는 비동기 제너레이터."""
    _log_context_tokens = set_log_context(chat_request.conv_id, chat_request.user_id)
    assistant_content = ""
    _ttft_logged = False
    t_flow_start = time.monotonic()
    _timings: dict = {f: "-" for f in _TIMING_FIELDS}
    _timings["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _timings["user_query"] = original_user_message[:200]
    _timings["use_rag"] = True
    _timings["clarification_question"] = ""
    _preprocess_to_persist: dict = {}
    _persist_referenced_documents: Optional[list] = None
    _history_saved = False

    try:
        logger.info(
            "[ChatFlow] conv_id=%s | user_id=%s | is_clarification=%s",
            chat_request.conv_id, chat_request.user_id, is_clarification,
        )
        yield f"data: {json.dumps({'conv_id': chat_request.conv_id}, ensure_ascii=False)}\n\n"

        use_rag = True
        user_intent = "general"
        reformed_query = None
        expanded_queries = None
        keywords = None
        search_target = None

        # ── "더 알려줘" 감지 ──────────────────────────────────────────────────
        # 되묻기 응답은 next_intent 분류기가 CLARIFY_REPLY로 식별해 더알려줘 분기 비활성.
        more = await _resolve_more_results_context(
            chat_request.conv_id,
            chat_request.messages,
            user_message,
            original_user_message,
            is_clarification_question=is_clarification,
        )
        if more.re_query:
            user_message = more.re_query
            logger.debug(
                "[MoreResults] conv_id=%s | 검색 전 질의 치환 완료 user_message=%s",
                chat_request.conv_id, shorten_text(user_message, 100),
            )

        # [1] PreCheck
        if not is_clarification:
            yield build_status_message("질문을 분석하고 있습니다")
        pre = await run_pre_check(user_message, chat_request.messages, is_clarification)
        use_rag = pre.use_rag
        _timings["t_pre_check"] = pre.elapsed
        _timings["use_rag"] = use_rag
        _timings["clarification_question"] = pre.clarification_question[:200] if pre.clarification_question else ""
        if not is_clarification:
            yield f"data: {json.dumps({'pre_check': {'use_rag': use_rag, 'clarification_question': pre.clarification_question}}, ensure_ascii=False)}\n\n"

        if pre.clarification_question:
            await asyncio.to_thread(_save_chat_history, chat_request, pre.clarification_question, original_user_message)
            clarify_resp = build_chat_response(
                response_message=pre.clarification_question,
                user_message=user_message,
                model_name=Config.MODEL_NAME,
                is_clarification=True,
            )
            assistant_content = pre.clarification_question
            if not _ttft_logged and t_request_start:
                _timings["t_ttft"] = round(time.monotonic() - t_request_start, 3)
                logger.info("[TTFT] 되묻기 첫 토큰까지 %.3fs", _timings["t_ttft"])
                _ttft_logged = True
            yield f"data: {json.dumps({'is_clarification': True}, ensure_ascii=False)}\n\n"
            async for chunk in _build_streaming_response(clarify_resp["choices"][0]["message"]["content"], clarify_resp):
                yield chunk
            return

        if not use_rag:
            yield build_status_message("답변 생성 중")
            response_message = await call_llm_api(
                message=user_message, system_prompt=load_system_prompt(), **_build_llm_kwargs(chat_request)
            )
            assistant_content = response_message
            response = build_chat_response(
                response_message=response_message,
                user_message=user_message,
                model_name=Config.MODEL_NAME,
                conv_id=chat_request.conv_id,
            )
            if not _ttft_logged and t_request_start:
                _timings["t_ttft"] = round(time.monotonic() - t_request_start, 3)
                logger.info("[TTFT] NO-RAG 첫 토큰까지 %.3fs", _timings["t_ttft"])
                _ttft_logged = True
            async for chunk in _build_streaming_response(response["choices"][0]["message"]["content"], response):
                yield chunk
            return

        # [2] 되묻기 답변의 시군 조기 확인
        resolved_sigun_filters = None
        early_sigun = run_early_sigun_check(user_message, chat_request.messages, use_rag, is_clarification)
        if early_sigun is not None:
            if early_sigun.need_clarify:
                if count_sigun_ask_attempts(chat_request.messages) >= MAX_SIGUN_ASK_ATTEMPTS:
                    await asyncio.to_thread(_save_chat_history, chat_request, MSG_SIGUN_FAILURE, original_user_message)
                    assistant_content = MSG_SIGUN_FAILURE
                    resp = build_chat_response(response_message=MSG_SIGUN_FAILURE, user_message=user_message, model_name=Config.MODEL_NAME)
                    async for chunk in _build_streaming_response(resp["choices"][0]["message"]["content"], resp):
                        yield chunk
                    return
                assistant_content = early_sigun.ask_message
                resp = build_chat_response(response_message=early_sigun.ask_message, user_message=user_message, model_name=Config.MODEL_NAME, is_clarification=True)
                yield f"data: {json.dumps({'is_clarification': True}, ensure_ascii=False)}\n\n"
                async for chunk in _build_streaming_response(resp["choices"][0]["message"]["content"], resp):
                    yield chunk
                return
            resolved_sigun_filters = early_sigun.filters
            logger.info("[SigunCheck/stream] 조기 확정(is_clarification): %s", resolved_sigun_filters)

        # [2.5] 분야 되묻기: 원질문이 정보량 0이면 query_recreation 전 1회 되묻기.
        if not more.detected and not more.topic_switch:
            _original_q_for_topic = get_second_last_user_message(chat_request.messages) or original_user_message
            topic_clarify = run_topic_clarify_check(
                _original_q_for_topic,
                chat_request.messages,
                use_rag,
                is_clarification,
                current_user_message=user_message,
            )
            if topic_clarify is not None and topic_clarify.need_clarify:
                logger.info("[TopicClarify/stream] 정보량 0 원질문 감지 → 분야 되묻기")
                await asyncio.to_thread(_save_chat_history, chat_request, topic_clarify.ask_message, original_user_message)
                assistant_content = topic_clarify.ask_message
                resp = build_chat_response(
                    response_message=topic_clarify.ask_message,
                    user_message=user_message,
                    model_name=Config.MODEL_NAME,
                    is_clarification=True,
                )
                yield f"data: {json.dumps({'is_clarification': True}, ensure_ascii=False)}\n\n"
                async for chunk in _build_streaming_response(resp["choices"][0]["message"]["content"], resp):
                    yield chunk
                return

        # [3] 쿼리 재구성 (NEW_SEARCH/REFINE_SEARCH면 직전 주제로 오염되지 않도록 스킵)
        if more.topic_switch:
            logger.info(
                "[Query Recreation] conv_id=%s | next_intent=NEW_SEARCH/REFINE_SEARCH → 재구성 스킵",
                chat_request.conv_id,
            )
        else:
            if is_clarification:
                yield build_status_message(STATUS_QUERY_RECREATION)
            _t = time.monotonic()
            user_message, _ = await _run_query_recreation(user_message, chat_request, is_clarification)
            _timings["t_query_recreation"] = round(time.monotonic() - _t, 3)
            logger.info("[TIMING] 쿼리 재구성: %.3fs", _timings["t_query_recreation"])

        # [4] 경상남도 외 지역 체크
        out_of_scope, region_name = run_out_of_scope_check(user_message, use_rag)
        if out_of_scope:
            msg = MSG_OUT_OF_SCOPE_TEMPLATE.format(region=region_name)
            assistant_content = msg
            resp = build_chat_response(response_message=msg, user_message=user_message, model_name=Config.MODEL_NAME, conv_id=chat_request.conv_id)
            async for chunk in _build_streaming_response(resp["choices"][0]["message"]["content"], resp):
                yield chunk
            return

        # [5] 시군 체크
        logger.info("[SigunCheck/stream] use_rag=%s, pre_resolved=%s", use_rag, resolved_sigun_filters is not None)
        sigun = run_sigun_check(user_message, chat_request.messages, use_rag, resolved_sigun_filters)
        if sigun is not None:
            if sigun.need_clarify:
                if count_sigun_ask_attempts(chat_request.messages) >= MAX_SIGUN_ASK_ATTEMPTS:
                    await asyncio.to_thread(_save_chat_history, chat_request, MSG_SIGUN_FAILURE, original_user_message)
                    assistant_content = MSG_SIGUN_FAILURE
                    resp = build_chat_response(response_message=MSG_SIGUN_FAILURE, user_message=user_message, model_name=Config.MODEL_NAME)
                    async for chunk in _build_streaming_response(resp["choices"][0]["message"]["content"], resp):
                        yield chunk
                    return
                assistant_content = sigun.ask_message
                resp = build_chat_response(response_message=sigun.ask_message, user_message=user_message, model_name=Config.MODEL_NAME, is_clarification=True)
                yield f"data: {json.dumps({'is_clarification': True}, ensure_ascii=False)}\n\n"
                async for chunk in _build_streaming_response(resp["choices"][0]["message"]["content"], resp):
                    yield chunk
                return
            resolved_sigun_filters = sigun.filters
            logger.info("[SigunCheck/stream] sigun_filters=%s", resolved_sigun_filters)

        # [6] 통합 전처리 (MORE_INFO면 히스토리 재사용 우선)
        # 배제 사업명(LLM 추출)은 unified_preprocess 호출 분기에서만 병렬 추출.
        # 히스토리 재사용 분기에서는 LLM 호출 없이 빈 리스트 유지.
        llm_excluded_services: list = []
        preprocess_data = None
        if more.detected and more.last_preprocess:
            if more.more_detail:
                user_message = (more.re_query or user_message or original_user_message).strip()
            else:
                user_message = more.last_preprocess["reformed_query"]
            await _update_user_message(chat_request.messages, user_message)
            previous_intent = str(more.last_preprocess.get("intent") or "general")
            reused_intent = resolve_reused_intent_on_more(
                previous_intent,
                more_detail=more.more_detail,
                user_message=user_message,
            )
            preprocess_data = _build_preprocess_from_history(
                user_message,
                more.last_preprocess,
                override_intent=reused_intent,
                intent_reason=(
                    "reused_from_history_on_more_detail"
                    if more.more_detail
                    else None
                ),
            )
            if more.more_detail:
                preprocess_data["expanded_queries"] = [user_message]
            logger.info(
                "[MoreResults] conv_id=%s | 히스토리 재사용 intent=%s (llm_more_detail=%s) | reformed=%s",
                chat_request.conv_id,
                preprocess_data.get("intent"),
                more.more_detail,
                shorten_text(user_message, 80),
            )
        elif more.blocked_followup and more.last_preprocess:
            # 직전 general 후속 — exclusion 없이 직전 reformed_query + intent 재사용
            re_query = str(more.last_preprocess.get("reformed_query") or "").strip() or user_message
            user_message = re_query
            await _update_user_message(chat_request.messages, user_message)
            preprocess_data = _build_preprocess_from_history(user_message, more.last_preprocess)
            logger.info(
                "[MoreResults/stream] general 후속 → 히스토리 intent 재사용 intent=%s reformed=%s (exclusion 없음)",
                more.last_preprocess.get("intent"),
                shorten_text(user_message, 80),
            )

        if preprocess_data is None:
            if llm_recommended_followup:
                pp = build_preprocess_skip_unified_recommended_question(user_message)
                logger.info("[ChatFlow] recommended-question API → unified_preprocess LLM 생략 (stream)")
            else:
                yield build_status_message("질문을 재구성하고 있습니다")
                # unified가 must_not_keywords를 함께 산출 → extract 호출 생략 (32B 1회 절약).
                pp = await run_unified_preprocess(
                    user_message, chat_request.messages, use_rag
                )
                llm_excluded_services = list(pp.must_not_keywords or [])
            _timings["t_unified_preprocess"] = pp.elapsed
            user_message = pp.query
            await _update_user_message(chat_request.messages, user_message)
            preprocess_data = {
                "query": pp.query, "intent": pp.intent, "intent_reason": pp.intent_reason,
                "reformed_query": pp.reformed_query, "expanded_queries": pp.expanded_queries,
                "keywords": pp.keywords,
                "search_target": pp.search_target,
                "policy_priority_tag": pp.policy_priority_tag,
                "lifecycle_tags": pp.lifecycle_tags,
                "household_tags": pp.household_tags,
                "detail_requested": pp.detail_requested,
            }

        user_intent      = preprocess_data["intent"]
        reformed_query   = preprocess_data["reformed_query"]
        expanded_queries = preprocess_data["expanded_queries"]
        keywords         = preprocess_data["keywords"]
        search_target    = preprocess_data.get("search_target")
        policy_priority_tag = preprocess_data.get("policy_priority_tag")
        lifecycle_tags   = preprocess_data.get("lifecycle_tags")
        household_tags   = preprocess_data.get("household_tags")

        # MORE_INFO는 직전 intent와 무관하게 guide_recommend로 강제한다.
        # (MORE_DETAIL은 기존 축 유지)
        if more.detected and not more.more_detail and user_intent != "guide_recommend":
            prev_intent = user_intent
            user_intent = "guide_recommend"
            preprocess_data["intent"] = "guide_recommend"
            prev_reason = (preprocess_data.get("intent_reason") or "").strip()
            suffix = "forced_guide_recommend_on_more_info(no_history_fallback)"
            preprocess_data["intent_reason"] = (
                f"{prev_reason} | {suffix}" if prev_reason else suffix
            )
            logger.info(
                "[MoreResults] conv_id=%s | MORE_INFO 강제 intent=guide_recommend (preprocess_intent_was=%s)",
                chat_request.conv_id,
                prev_intent,
            )

        if llm_recommended_followup and user_intent != "guide_recommend":
            _unified_intent = user_intent
            user_intent = "guide_recommend"
            preprocess_data["intent"] = "guide_recommend"
            prev_reason = (preprocess_data.get("intent_reason") or "").strip()
            suffix = "forced_guide_recommend(recommended_question_api)"
            preprocess_data["intent_reason"] = (
                f"{prev_reason} | {suffix}" if prev_reason else suffix
            )
            logger.info(
                "[ChatFlow] recommended-question API → RAG intent=guide_recommend (preprocess_intent_was=%s)",
                _unified_intent,
            )

        yield f"data: {json.dumps({'chat-intent': user_intent})}\n\n"
        yield f"data: {json.dumps({'preprocess': {'query': preprocess_data.get('query', ''), 'intent': user_intent, 'intent_reason': preprocess_data.get('intent_reason', ''), 'reformed_query': reformed_query, 'expanded_queries': expanded_queries, 'search_target': search_target, 'policy_priority_tag': policy_priority_tag}}, ensure_ascii=False)}\n\n"
        _preprocess_to_persist = {
            "query": preprocess_data.get("query", ""), "intent": user_intent,
            "reformed_query": reformed_query, "expanded_queries": expanded_queries,
            "more_info": bool(more.detected),
            "search_target": search_target,
            "policy_priority_tag": policy_priority_tag,
        }
        _capture_preprocess_timings(_timings, preprocess_data, expanded_queries)

        # [7] 생애주기 체크 (requires_lifecycle=True 인 intent 전용, 로그만)
        _t_lc = time.monotonic()
        run_lifecycle_check(user_message, chat_request.messages, use_rag, user_intent)
        _intent_spec = _lookup_intent_spec(user_intent)
        if use_rag and _intent_spec.requires_lifecycle:
            _timings["t_lifecycle_check"] = round(time.monotonic() - _t_lc, 3)

        logger.info("[Intent Classification] intent=%s", user_intent)

        # [8] RAG 태스크 실행
        status_queue = asyncio.Queue()

        async def emit_status(message: str):
            await status_queue.put(message)

        llm_kwargs = _build_llm_kwargs(chat_request)
        rag_processor = _get_rag_processor(
            user_intent, recommended_question_route=llm_recommended_followup
        )
        _t_rag = time.monotonic()
        _rag_kwargs = dict(
            message=user_message,
            reformed_query=reformed_query,
            stream=True,
            status_callback=emit_status,
            intent=user_intent,
            messages=chat_request.messages,
            sigun_filters=resolved_sigun_filters,
            precomputed_expanded_queries=expanded_queries,
            precomputed_keywords=keywords,
            precomputed_search_target=search_target,
            precomputed_policy_priority_tag=policy_priority_tag,
            precomputed_lifecycle_tags=lifecycle_tags,
            precomputed_household_tags=household_tags,
            service_target=getattr(chat_request, "service_target", None) or "official",
            excluded_chunk_ids=more.excluded_chunk_ids,
            excluded_service_names=more.excluded_service_names,
            llm_excluded_services=llm_excluded_services,
            final_user_message=more.final_user_message,
            **{k: v for k, v in llm_kwargs.items() if k != "messages"},
        )
        if _intent_spec.supports_detail_form:
            # MORE_DETAIL 후속(이전 대화 기반) 또는 unified_preprocess의 detail_requested(첫 메시지에서 자세히 요청) 중 하나라도 True면 form B 강제
            effective_more_detail = bool(more.more_detail) or bool(preprocess_data.get("detail_requested"))
            if effective_more_detail and not more.more_detail:
                logger.info(
                    "[MoreResults] conv_id=%s | detail_requested=True from unified_preprocess → more_detail 활성화",
                    chat_request.conv_id,
                )
            _rag_kwargs["more_detail"] = effective_more_detail
        rag_task = asyncio.create_task(rag_processor(**_rag_kwargs))
        if more.detected:
            logger.info(
                "[MoreResults] conv_id=%s | RAG 호출 전달값 excluded_chunk_ids=%d | excluded_service_names=%d | query=%s | reformed=%s",
                chat_request.conv_id,
                len(more.excluded_chunk_ids),
                len(more.excluded_service_names),
                shorten_text(user_message or "", 80),
                shorten_text(reformed_query or "", 80),
            )

        async for status_msg in drain_status_until_done(rag_task, status_queue):
            yield build_status_message(status_msg)

        try:
            result, referenced_documents = await rag_task
            _timings["t_rag_total"] = round(time.monotonic() - _t_rag, 3)
            logger.info("[TIMING] RAG 처리 전체(intent=%s): %.3fs", user_intent, _timings["t_rag_total"])
        except Exception as rag_error:
            logger.error(f"[RAG Task Error] {rag_error}", exc_info=True)
            error_msg = "검색 처리 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
            async for chunk in _stream_delta_content(error_msg):
                yield chunk
            yield "data: [DONE]\n\n"
            return

        if isinstance(result, str):
            assistant_content = result
            if not _ttft_logged and t_request_start:
                _ttft = round(time.monotonic() - t_request_start, 3)
                _timings["t_ttft"] = _ttft
                logger.info("[TTFT] RAG 첫 토큰까지 %.3fs", _ttft)
                _ttft_logged = True
            async for chunk in _stream_delta_content(assistant_content):
                yield chunk
        else:
            try:
                async for chunk in result:
                    if isinstance(chunk, str) and chunk.startswith("data: "):
                        data_content = chunk[len("data: "):].strip()
                        # LLM [DONE]은 억제 — 참고문서 전송 후 streaming.py에서 직접 [DONE] 전송
                        if data_content == "[DONE]":
                            continue
                        if not _ttft_logged and t_request_start:
                            _ttft = round(time.monotonic() - t_request_start, 3)
                            _timings["t_ttft"] = _ttft
                            logger.info("[TTFT] RAG(stream) 첫 토큰까지 %.3fs", _ttft)
                            _ttft_logged = True
                        yield chunk
                        if data_content:
                            try:
                                payload = json.loads(data_content)
                                content = payload.get("choices", [{}])[0].get("delta", {}).get("content")
                                if content:
                                    assistant_content += content
                            except Exception:
                                pass
            except Exception as stream_error:
                logger.error(f"[RAG Stream Error] 스트리밍 중 오류 발생: {stream_error}", exc_info=True)
                error_msg = "답변 생성 중 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
                async for chunk in _stream_delta_content(error_msg):
                    yield chunk
                yield "data: [DONE]\n\n"
                return

        raw_referenced_documents = list(referenced_documents or [])
        # 응답 본문 alias 매칭 필터 비활성화: LLM이 행정 접미사를 생략하면 alias 매칭이 실패해
        # 실제 참조 문서가 UI에서 누락되는 false negative 발생. RelevanceFilter(8b/sllm)가 이미
        # 비관련 문서를 걸렀으므로 그 결과를 그대로 UI 카드와 히스토리에 사용.
        filtered_referenced_documents = raw_referenced_documents
        _persist_referenced_documents = raw_referenced_documents

        if filtered_referenced_documents:
            yield f"data: {json.dumps({'referenced_documents': filtered_referenced_documents}, ensure_ascii=False)}\n\n"

        # NOTE:
        # 클라이언트가 [DONE] 직후 연결을 닫으면 finally 블록의 await 저장이 취소될 수 있다.
        # 따라서 [DONE] 전 선저장을 시도하고, finally는 보조 저장으로만 동작한다.
        try:
            _history_saved = await asyncio.shield(
                asyncio.to_thread(
                    _save_stream_history,
                    chat_request,
                    original_user_message,
                    assistant_content,
                    _preprocess_to_persist or None,
                    _persist_referenced_documents,
                )
            )
        except Exception as save_err:
            logger.error("[History Save/stream] 실패: %s", save_err, exc_info=True)
            _history_saved = False

        yield "data: [DONE]\n\n"

    finally:
        _timings["t_total_flow"] = round(time.monotonic() - t_flow_start, 3)
        _timings["assistant_response"] = assistant_content
        logger.info("[TIMING] 스트리밍 전체 흐름: %.3fs", _timings["t_total_flow"])
        _summary = " | ".join(
            f"{k.replace('t_', '')}={v}s"
            for k, v in _timings.items()
            if k.startswith("t_") and v != "-"
        )
        logger.info("[TIMING 요약] query=%s | %s", original_user_message[:40], _summary)
        try:
            await asyncio.to_thread(_write_timing_csv, _timings)
        except Exception as e:
            logger.warning("[TIMING CSV] 기록 실패 (파일 잠금?): %s", e)
        if not _history_saved:
            try:
                await asyncio.shield(
                    asyncio.to_thread(
                        _save_stream_history,
                        chat_request,
                        original_user_message,
                        assistant_content,
                        _preprocess_to_persist or None,
                        _persist_referenced_documents,
                    )
                )
            except Exception as save_err:
                logger.error("[History Save/stream/finally] 실패: %s", save_err, exc_info=True)
        reset_log_context(_log_context_tokens)
