"""스트리밍 채팅 응답 흐름"""

import asyncio
import csv
import json
import logging
import os
import time
from datetime import datetime
from typing import AsyncGenerator, Optional

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
from app.shared.utils import build_chat_response, load_system_prompt
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
from app.shared.utils.keyword_extractor import extract_nouns
from app.chat.infra.rag import filter_okms_keywords
from app.chat.intent_registry import lookup as _lookup_intent_spec

logger = logging.getLogger(__name__)

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

        # step6: 후속 의도분류(classify_next_intent/more_results) 제거.
        # 모든 턴은 재작성→분류 단일 경로. "더 알려줘"는 전량 노출 폐지, 더보기=프론트 칩 드릴다운.

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

        # [3] 쿼리 재구성
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

        # [6] 통합 전처리 (step6: 후속 히스토리 재사용 분기 폐지 — 항상 전처리)
        llm_excluded_services: list = []
        preprocess_data = None
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
                "lifecycle_tags": pp.lifecycle_tags,
                "household_tags": pp.household_tags,
                "topic_category": pp.topic_category,
                "topic_keyword": pp.topic_keyword,
                "must_not_keywords": pp.must_not_keywords,
                "detail_requested": pp.detail_requested,
            }

        user_intent      = preprocess_data["intent"]
        reformed_query   = preprocess_data["reformed_query"]
        expanded_queries = preprocess_data["expanded_queries"]
        keywords         = preprocess_data["keywords"]
        search_target    = preprocess_data.get("search_target")
        lifecycle_tags   = preprocess_data.get("lifecycle_tags")
        household_tags   = preprocess_data.get("household_tags")
        topic_category   = preprocess_data.get("topic_category")
        topic_keyword    = preprocess_data.get("topic_keyword")
        must_not_keywords = preprocess_data.get("must_not_keywords")

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
        yield f"data: {json.dumps({'preprocess': {'query': preprocess_data.get('query', ''), 'intent': user_intent, 'intent_reason': preprocess_data.get('intent_reason', ''), 'reformed_query': reformed_query, 'expanded_queries': expanded_queries, 'search_target': search_target}}, ensure_ascii=False)}\n\n"
        _preprocess_to_persist = {
            "query": preprocess_data.get("query", ""), "intent": user_intent,
            "reformed_query": reformed_query, "expanded_queries": expanded_queries,
            "more_info": False,
            "search_target": search_target,
        }
        for _k, _v in (  # B1 슬롯 영속: 후속턴(칩 드릴다운) 컨텍스트 복원용
            ("sigun_filters", resolved_sigun_filters), ("lifecycle_tags", lifecycle_tags),
            ("household_tags", household_tags), ("topic_category", topic_category),
            ("topic_keyword", topic_keyword), ("must_not_keywords", must_not_keywords),
        ):
            if _v:
                _preprocess_to_persist[_k] = list(_v)
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
            precomputed_lifecycle_tags=lifecycle_tags,
            precomputed_household_tags=household_tags,
            precomputed_topic_category=topic_category,
            precomputed_topic_keyword=topic_keyword,
            precomputed_must_not_keywords=must_not_keywords,
            service_target=getattr(chat_request, "service_target", None) or "official",
            excluded_chunk_ids=[],
            excluded_service_names=[],
            llm_excluded_services=llm_excluded_services,
            final_user_message=None,
            **{k: v for k, v in llm_kwargs.items() if k != "messages"},
        )
        if _intent_spec.supports_detail_form:
            # unified_preprocess의 detail_requested(첫 메시지에서 자세히 요청)면 form B 강제
            _rag_kwargs["more_detail"] = bool(preprocess_data.get("detail_requested"))
        guide_meta: dict = {}
        if user_intent == "guide_recommend":
            _rag_kwargs["out_meta"] = guide_meta   # DB 경로 topic_chips 수신용
        rag_task = asyncio.create_task(rag_processor(**_rag_kwargs))

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
        if guide_meta.get("topic_chips"):
            # B2: 칩(topic_chips) + 검색 슬롯(slots)을 함께 내보내 stateless 드릴다운 계약 구성.
            # 프론트는 chip 클릭 시 {**slots, topic_category: chip.label} 로 drilldown 요청을 재전송한다.
            yield f"data: {json.dumps({'topic_chips': guide_meta['topic_chips'], 'slots': guide_meta.get('slots')}, ensure_ascii=False)}\n\n"

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
