"""스트리밍 채팅 응답 흐름"""

import asyncio
import csv
import json
import logging
import os
import time
from datetime import datetime
from typing import AsyncGenerator

from core.config import Config
from core.models import ChatRequest
from core.logging_context import set_log_context, reset_log_context
from services import (
    call_llm_api,
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
    load_system_prompt,
    shorten_text,
)
from utils.status_messages import (
    build_status_message,
    STATUS_QUERY_RECREATION,
)
from ..deps import (
    _build_llm_kwargs,
    _build_streaming_response,
    _filter_referenced_documents_by_response,
    _save_chat_history,
    _stream_delta_content,
    _update_user_message,
)
from .helpers import _get_rag_processor, _run_query_recreation
from .conversation import _save_stream_history
from services.more_results_service import (
    get_excluded_info_from_history,
    get_base_user_query_from_history,
    get_last_preprocess_from_history,
)
from utils.keyword_extractor import extract_nouns
from services.rag_service import filter_okms_keywords
from services.router_service import classify_next_intent

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


async def _streaming_chat_flow(
    chat_request: ChatRequest,
    user_message: str,
    original_user_message: str,
    is_clarification: bool,
    t_request_start: float = 0.0,
) -> AsyncGenerator[str, None]:
    """스트리밍 채팅 응답 전체 흐름을 처리하는 비동기 제너레이터."""
    _log_context_tokens = set_log_context(chat_request.conv_id, chat_request.user_id)
    assistant_content = ""
    _referenced_chunk_ids: list = []
    _referenced_service_names: list = []
    _ttft_logged = False
    t_flow_start = time.monotonic()
    _timings: dict = {f: "-" for f in _TIMING_FIELDS}
    _timings["timestamp"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _timings["user_query"] = original_user_message[:200]
    _preprocess_to_persist: dict = {}
    # PreCheck 기본값 (is_clarification 경로에서는 pre_check 스킵)
    _timings["use_rag"] = True
    _timings["clarification_question"] = ""
    try:
        logger.info(
            "[ChatFlow] conv_id=%s | user_id=%s | is_clarification=%s",
            chat_request.conv_id,
            chat_request.user_id,
            is_clarification,
        )
        yield f"data: {json.dumps({'conv_id': chat_request.conv_id}, ensure_ascii=False)}\n\n"

        # is_clarification=True 경로를 위한 기본값 초기화
        use_rag = True
        user_intent = "general"
        reformed_query = None
        expanded_queries = None
        keywords = None

        # ── "더 알려줘" 감지: 이전 응답 chunk_ids/서비스명 추출 ───────────────
        excluded_chunk_ids: list = []
        excluded_service_names: list = []
        more_results_re_query: str | None = None
        more_info_final_user_message: str | None = None
        last_preprocess = get_last_preprocess_from_history(chat_request.messages)
        more_results_detected = False
        base_user_query = get_base_user_query_from_history(chat_request.messages)
        next_intent = None
        if not more_results_detected:
            # 패턴 매칭 없이 LLM 분류로만 MORE_INFO를 감지
            next_intent = await classify_next_intent(chat_request.messages, user_message)
            more_results_detected = (next_intent.get("intent") == "MORE_INFO")
            logger.info(
                "[MoreResults] conv_id=%s | 보조분류 next_intent=%s | re_query=%s",
                chat_request.conv_id,
                next_intent.get("intent"),
                shorten_text(str(next_intent.get("re_query", "") or ""), 80),
            )

        if more_results_detected:
            # MORE_INFO는 사용자 원문 대신 "직전 문서검색에 사용된 질의(reformed_query)"를 우선 재사용
            if last_preprocess and last_preprocess.get("reformed_query"):
                more_results_re_query = str(last_preprocess.get("reformed_query", "")).strip()
                logger.info(
                    "[MoreResults] conv_id=%s | 검색질의 재사용(reformed_query): %s",
                    chat_request.conv_id,
                    shorten_text(more_results_re_query, 80),
                )
            elif base_user_query:
                more_results_re_query = base_user_query
                logger.info(
                    "[MoreResults] conv_id=%s | fallback 원질문 재사용: %s",
                    chat_request.conv_id,
                    shorten_text(more_results_re_query, 80),
                )
            elif next_intent and next_intent.get("intent") == "MORE_INFO":
                re_query = str(next_intent.get("re_query", "") or "").strip()
                if re_query:
                    more_results_re_query = re_query
                    logger.info(
                        "[MoreResults] conv_id=%s | fallback re_query 적용: %s",
                        chat_request.conv_id,
                        shorten_text(more_results_re_query, 80),
                    )
            excluded_chunk_ids, excluded_service_names = get_excluded_info_from_history(
                chat_request.messages
            )
            logger.info(
                "[MoreResults] conv_id=%s | 제외 대상 집계 chunk_ids=%d | service_names=%d",
                chat_request.conv_id,
                len(excluded_chunk_ids),
                len(excluded_service_names),
            )
            logger.info(
                "[MoreResults] conv_id=%s | 제외 샘플 chunk_ids=%s | service_names=%s",
                chat_request.conv_id,
                excluded_chunk_ids[:10],
                excluded_service_names[:10],
            )
            _llm_rq = str((next_intent or {}).get("llm_re_query", "") or "").strip()
            more_info_final_user_message = _llm_rq or (original_user_message or "").strip() or None
            logger.info(
                "[MoreResults] conv_id=%s | 최종LLM user 문구(next_intent llm_re_query 우선)=%s",
                chat_request.conv_id,
                shorten_text(more_info_final_user_message or "", 100),
            )
        # ──────────────────────────────────────────────────────────────────────

        if more_results_re_query:
            # 검색 전 단계에서 후속질문을 완성 질의로 치환
            user_message = more_results_re_query
            logger.info(
                "[MoreResults] conv_id=%s | 검색 전 질의 치환 완료 user_message=%s",
                chat_request.conv_id,
                shorten_text(user_message, 100),
            )

        # ── [1단계] 선행 판단: use_rag + 되묻기 (is_clarification이면 스킵) ──
        if not is_clarification:
            yield build_status_message("질문을 분석하고 있습니다")

            _t = time.monotonic()
            # MORE_INFO: 검색용 user_message가 '밀양' 등으로만 치환되면 pre_check가 NO-RAG를 내기 쉬움.
            # 히스토리 두께와 무관히 로그인과 동일하게 항상 RAG로 진행한다.
            if more_results_detected:
                use_rag = True
                clarification_question = ""
                logger.info(
                    "[MoreResults] conv_id=%s | pre_check 생략(MORE_INFO) → use_rag=True",
                    chat_request.conv_id,
                )
            else:
                pre_check_result = await pre_check(user_message, chat_request.messages)
                use_rag = pre_check_result["use_rag"]
                clarification_question = pre_check_result["clarification_question"]
            _timings["t_pre_check"] = round(time.monotonic() - _t, 3)
            logger.info("[TIMING] 선행 판단(pre_check): %.3fs", _timings["t_pre_check"])
            _timings["use_rag"] = use_rag
            _timings["clarification_question"] = clarification_question[:200] if clarification_question else ""

            # 클라이언트에 pre_check 결과 전송 (테스트/로깅용)
            yield f"data: {json.dumps({'pre_check': {'use_rag': use_rag, 'clarification_question': clarification_question}}, ensure_ascii=False)}\n\n"

            if clarification_question:
                await asyncio.to_thread(_save_chat_history, chat_request, clarification_question, original_user_message)
                clarify_response = build_chat_response(
                    response_message=clarification_question,
                    user_message=user_message,
                    model_name=Config.MODEL_NAME,
                    is_clarification=True,
                )
                assistant_content = clarification_question
                if not _ttft_logged and t_request_start:
                    _ttft = round(time.monotonic() - t_request_start, 3)
                    _timings["t_ttft"] = _ttft
                    logger.info("[TTFT] 되묻기 첫 토큰까지 %.3fs", _ttft)
                    _ttft_logged = True
                yield f"data: {json.dumps({'is_clarification': True}, ensure_ascii=False)}\n\n"
                async for chunk in _build_streaming_response(
                    clarify_response["choices"][0]["message"]["content"], clarify_response
                ):
                    yield chunk
                return

            if not use_rag:
                yield build_status_message("답변 생성 중")
                logger.info("[NO-RAG Mode] 질문: %s", shorten_text(user_message, 100))
                system_prompt = load_system_prompt()
                response_message = await call_llm_api(
                    message=user_message,
                    system_prompt=system_prompt,
                    **_build_llm_kwargs(chat_request)
                )
                assistant_content = response_message
                response = build_chat_response(
                    response_message=response_message,
                    user_message=user_message,
                    model_name=Config.MODEL_NAME,
                    conv_id=chat_request.conv_id,
                )
                if not _ttft_logged and t_request_start:
                    _ttft = round(time.monotonic() - t_request_start, 3)
                    _timings["t_ttft"] = _ttft
                    logger.info("[TTFT] NO-RAG 첫 토큰까지 %.3fs", _ttft)
                    _ttft_logged = True
                async for chunk in _build_streaming_response(
                    response["choices"][0]["message"]["content"], response
                ):
                    yield chunk
                return
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
                    await asyncio.to_thread(_save_chat_history, chat_request, MSG_SIGUN_FAILURE, original_user_message)
                    assistant_content = MSG_SIGUN_FAILURE
                    response = build_chat_response(response_message=MSG_SIGUN_FAILURE, user_message=user_message, model_name=Config.MODEL_NAME)
                    async for chunk in _build_streaming_response(response["choices"][0]["message"]["content"], response):
                        yield chunk
                    return
                assistant_content = _pre_ask_msg
                response = build_chat_response(response_message=_pre_ask_msg, user_message=user_message, model_name=Config.MODEL_NAME, is_clarification=True)
                yield f"data: {json.dumps({'is_clarification': True}, ensure_ascii=False)}\n\n"
                async for chunk in _build_streaming_response(response["choices"][0]["message"]["content"], response):
                    yield chunk
                return
            # 시군 확정 → 이후 check_sigun 재실행 불필요
            resolved_sigun_filters = _pre_filters if _pre_filters else None
            logger.info(f"[SigunCheck/stream] 조기 확정(is_clarification): {resolved_sigun_filters}")
        # ─────────────────────────────────────────────────────────────────────

        if is_clarification:
            yield build_status_message(STATUS_QUERY_RECREATION)
        _t = time.monotonic()
        user_message, skip_clarification_check = await _run_query_recreation(
            user_message, chat_request, is_clarification
        )
        _timings["t_query_recreation"] = round(time.monotonic() - _t, 3)
        logger.info("[TIMING] 쿼리 재구성: %.3fs", _timings["t_query_recreation"])

        # ── 경상남도 외 지역 체크 ─────────────────────────────────────────────
        if use_rag:
            out_of_scope, region_name = check_out_of_scope_region(user_message)
            if out_of_scope:
                msg = MSG_OUT_OF_SCOPE_TEMPLATE.format(region=region_name)
                assistant_content = msg
                response = build_chat_response(
                    response_message=msg,
                    user_message=user_message,
                    model_name=Config.MODEL_NAME,
                    conv_id=chat_request.conv_id,
                )
                async for chunk in _build_streaming_response(
                    response["choices"][0]["message"]["content"], response
                ):
                    yield chunk
                return
        # ────────────────────────────────────────────────────────────────────

        # ── 시군 체크 ─────────────────────────────────────────────────────────
        # resolved_sigun_filters는 is_clarification=True 조기 확인에서 이미 설정될 수 있음
        logger.info(f"[SigunCheck/stream] use_rag={use_rag}, is_clarification={is_clarification}, pre_resolved={resolved_sigun_filters is not None}")
        if use_rag and resolved_sigun_filters is None:
            _t = time.monotonic()
            sigun_filters, need_sigun_clarify, sigun_ask_msg = check_sigun(
                user_message, chat_request.messages
            )
            logger.info("[TIMING] 시군 체크: %.3fs", time.monotonic() - _t)  # CSV 미포함(경량)
            if need_sigun_clarify:
                sigun_ask_count = count_sigun_ask_attempts(chat_request.messages)
                if sigun_ask_count >= MAX_SIGUN_ASK_ATTEMPTS:
                    response = build_chat_response(
                        response_message=MSG_SIGUN_FAILURE,
                        user_message=user_message,
                        model_name=Config.MODEL_NAME,
                    )
                    await asyncio.to_thread(_save_chat_history, chat_request, MSG_SIGUN_FAILURE, original_user_message)
                    assistant_content = MSG_SIGUN_FAILURE
                    async for chunk in _build_streaming_response(
                        response["choices"][0]["message"]["content"], response
                    ):
                        yield chunk
                    return
                response = build_chat_response(
                    response_message=sigun_ask_msg,
                    user_message=user_message,
                    model_name=Config.MODEL_NAME,
                    is_clarification=True,
                )
                assistant_content = sigun_ask_msg
                yield f"data: {json.dumps({'is_clarification': True}, ensure_ascii=False)}\n\n"
                async for chunk in _build_streaming_response(
                    response["choices"][0]["message"]["content"], response
                ):
                    yield chunk
                return
            resolved_sigun_filters = sigun_filters if sigun_filters else None
            logger.info(f"[SigunCheck/stream] sigun_filters={resolved_sigun_filters}")
        # ──────────────────────────────────────────────────────────────────────

        # ── [2단계] 통합 전처리 (MORE_INFO면 히스토리 재사용 우선) ──────────────
        preprocess = None
        reused_preprocess = None
        if more_results_detected:
            reused_preprocess = last_preprocess
            if reused_preprocess:
                user_intent = reused_preprocess["intent"]
                reformed_query = reused_preprocess["reformed_query"]
                user_message = reformed_query
                await _update_user_message(chat_request.messages, user_message)
                expanded_queries = reused_preprocess["expanded_queries"] or [reformed_query]
                try:
                    keywords = extract_nouns(reformed_query)
                except Exception:
                    keywords = []
                preprocess = {
                    "query": user_message,
                    "intent": user_intent,
                    "intent_reason": "reused_from_history_on_more_info",
                    "reformed_query": reformed_query,
                    "expanded_queries": expanded_queries,
                    "keywords": keywords,
                }
                logger.info(
                    "[MoreResults] conv_id=%s | unified_preprocess 스킵, 히스토리 재사용 intent=%s | reformed=%s | expanded=%d",
                    chat_request.conv_id,
                    user_intent,
                    shorten_text(reformed_query or "", 80),
                    len(expanded_queries or []),
                )

        if preprocess is None:
            yield build_status_message("질문을 재구성하고 있습니다")
            _t = time.monotonic()
            _preprocess_messages = None if skip_clarification_check else chat_request.messages
            preprocess = await unified_preprocess(user_message, _preprocess_messages, use_rag=use_rag)
            _timings["t_unified_preprocess"] = round(time.monotonic() - _t, 3)
            logger.info("[TIMING] 통합 전처리(unified_preprocess): %.3fs", _timings["t_unified_preprocess"])

            user_message = preprocess["query"]
            await _update_user_message(chat_request.messages, user_message)

            user_intent      = preprocess["intent"]
            reformed_query   = preprocess["reformed_query"]
            expanded_queries = preprocess["expanded_queries"]
            keywords         = preprocess["keywords"]
            if more_results_detected:
                logger.info(
                    "[MoreResults] conv_id=%s | preprocess 결과 intent=%s | reformed=%s | expanded=%d",
                    chat_request.conv_id,
                    user_intent,
                    shorten_text(reformed_query or "", 80),
                    len(expanded_queries or []),
                )

        yield f"data: {json.dumps({'chat-intent': preprocess['intent']})}\n\n"
        # 클라이언트에 preprocess 전체 결과 전송 (테스트/로깅용)
        yield f"data: {json.dumps({'preprocess': {'query': preprocess.get('query', ''), 'intent': preprocess.get('intent', ''), 'intent_reason': preprocess.get('intent_reason', ''), 'reformed_query': preprocess.get('reformed_query', ''), 'expanded_queries': preprocess.get('expanded_queries', [])}}, ensure_ascii=False)}\n\n"
        _preprocess_to_persist = {
            "query": preprocess.get("query", ""),
            "intent": preprocess.get("intent", ""),
            "intent_reason": preprocess.get("intent_reason", ""),
            "reformed_query": preprocess.get("reformed_query", ""),
            "expanded_queries": preprocess.get("expanded_queries", []),
            # RAG strategy intent와 별도: 이번 응답이 MORE_INFO(이전에 이어 '더 보기') 턴이면 True.
            # more_results_service 누적 제외(히스토리 walk)에만 쓰임.
            "more_info": bool(more_results_detected),
        }

        # ── CSV 캡처: UnifiedPreprocess 결과 ─────────────────────────────────
        _timings["query"]           = preprocess.get("query", "")[:200]
        _timings["intent"]          = user_intent
        _timings["intent_reason"]   = preprocess.get("intent_reason", "")[:200]
        _timings["reformed_query"]  = (reformed_query or "")[:200]
        _timings["expanded_queries"] = " | ".join(expanded_queries or [])
        # 키워드 검색어: 벡터 검색어에서 kiwi 명사 추출 (pipeline과 동일 로직)
        _kw_strings = [
            " ".join(filter_okms_keywords(extract_nouns(eq, use_bigram=False)))
            for eq in (expanded_queries or [])
        ]
        for _i, _kw in enumerate(_kw_strings[:5], 1):
            _timings[f"kw_{_i}"] = _kw
        # ──────────────────────────────────────────────────────────────────────

        # ── 생애주기 체크 (guide_recommend 전용) ─────────────────────────────
        if use_rag and user_intent == "guide_recommend":
            _t = time.monotonic()
            lifecycle, need_lifecycle_clarify, _ = check_lifecycle(
                user_message, chat_request.messages
            )
            _timings["t_lifecycle_check"] = round(time.monotonic() - _t, 3)
            logger.info("[TIMING] 생애주기 체크: %.3fs", _timings["t_lifecycle_check"])
            if need_lifecycle_clarify:
                logger.info("[LifecycleCheck/stream] 생애주기 미확인 → 되묻기 없이 진행")
            else:
                logger.info(f"[LifecycleCheck/stream] 생애주기 확인: '{lifecycle}'")
        # ──────────────────────────────────────────────────────────────────────

        logger.info("[Intent Classification] intent=%s", user_intent)

        status_queue = asyncio.Queue()

        async def emit_status(message: str):
            await status_queue.put(message)

        llm_kwargs = _build_llm_kwargs(chat_request)
        rag_processor = _get_rag_processor(user_intent)
        _t_rag = time.monotonic()
        rag_task = asyncio.create_task(
            rag_processor(
                message=user_message,
                reformed_query=reformed_query,
                stream=True,
                status_callback=emit_status,
                intent=user_intent,
                messages=chat_request.messages,
                sigun_filters=resolved_sigun_filters,
                precomputed_expanded_queries=expanded_queries,
                precomputed_keywords=keywords,
                excluded_chunk_ids=excluded_chunk_ids,
                excluded_service_names=excluded_service_names,
                final_user_message=more_info_final_user_message,
                **{k: v for k, v in llm_kwargs.items() if k != "messages"}
            )
        )
        if more_results_detected:
            logger.info(
                "[MoreResults] conv_id=%s | RAG 호출 전달값 excluded_chunk_ids=%d | excluded_service_names=%d | query=%s | reformed=%s",
                chat_request.conv_id,
                len(excluded_chunk_ids or []),
                len(excluded_service_names or []),
                shorten_text(user_message or "", 80),
                shorten_text(reformed_query or "", 80),
            )
        while True:
            if rag_task.done() and status_queue.empty():
                break
            try:
                status_msg = await asyncio.wait_for(status_queue.get(), timeout=0.2)
                yield build_status_message(status_msg)
            except asyncio.TimeoutError:
                pass

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

        # 전체 chunk_ids/서비스명 캡처 (더 알려줘 제외 목록용) — UI 슬라이싱 전
        _referenced_chunk_ids = [d.get("chunk_id", "") for d in referenced_documents if d.get("chunk_id")]
        _referenced_service_names = [d.get("name", "") for d in referenced_documents if d.get("name")]

        referenced_documents = _filter_referenced_documents_by_response(
            assistant_content,
            referenced_documents,
        )

        if referenced_documents:
            yield f"data: {json.dumps({'referenced_documents': referenced_documents}, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"

    finally:
        _timings["t_total_flow"] = round(time.monotonic() - t_flow_start, 3)
        _timings["assistant_response"] = assistant_content
        logger.info("[TIMING] 스트리밍 전체 흐름: %.3fs", _timings["t_total_flow"])
        # ── TIMING 요약 ────────────────────────────────────────────────────────
        _summary = " | ".join(
            f"{k.replace('t_', '')}={v}s"
            for k, v in _timings.items()
            if k.startswith("t_") and v != "-"
        )
        logger.info("[TIMING 요약] query=%s | %s", original_user_message[:40], _summary)
        # ── CSV 기록 ───────────────────────────────────────────────────────────
        try:
            await asyncio.to_thread(_write_timing_csv, _timings)
        except Exception as e:
            logger.warning("[TIMING CSV] 기록 실패 (파일 잠금?): %s", e)
        await asyncio.to_thread(
            _save_stream_history,
            chat_request,
            original_user_message,
            assistant_content,
            _referenced_chunk_ids or None,
            _referenced_service_names or None,
            _preprocess_to_persist or None,
        )
        reset_log_context(_log_context_tokens)
