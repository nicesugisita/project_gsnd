"""
RAG 서비스 — search 전용 플로우

search 의도(기관/시설 정보 조회)의 검색 및 응답 생성을 담당합니다.
통합 전처리 `search_target`에 따라 Mariner 검색은 OUR_REGION_TEL 또는 WELFARE_CENTER **한 풀만** 실행합니다.
"""

import logging
import asyncio
import time
from typing import Any, Dict, List, Optional

# Mariner 쿼리셋
# GSND_WELFARE_CENTER 비활성화 시 아래 import·_run_welfare_center_query 주석 블록을 함께 복구
# from app.mariner.queryset_welfare import query_welfare_center_documents
from app.mariner.queryset_welfare_tel import query_welfare_tel_documents

from app.chat.infra.rag import (
    _deduplicate_documents,
    _get_document_name,
    _build_search_queries,
)
from .common import (
    dedupe_cap_expanded_queries,
    apply_policy_priority_to_documents,
)
from .pipeline_utils import (
    build_welfare_search_queries,
    collect_welfare_center_tel_docs,
    log_step_banner,
)
from .response_generator import generate_final_response_v2
from app.chat.routing import (
    expand_query,
    extract_triples,
)
from app.shared.utils.relevance_filter import filter_irrelevant_docs

logger = logging.getLogger(__name__)


def _search_pool_tag_from_target(precomputed_search_target: Optional[str]) -> str:
    """전처리 search_target → 검색 블록 키 (welfare_center | our_region_tel)."""
    hint = str(precomputed_search_target or "").strip().lower().replace("-", "_")

    if hint == "welfare_facility":
        return "welfare_center"
    if hint == "admin_local_office":
        return "our_region_tel"
    if hint in ("ambiguous", ""):
        logger.info(
            "[RAG/search_v2] search_target 미전달·ambiguous → OUR_REGION_TEL 단일 Mariner 검색"
        )
        return "our_region_tel"
    logger.warning(
        "[RAG/search_v2] 알 수 없는 search_target=%r → OUR_REGION_TEL로 검색",
        precomputed_search_target,
    )
    return "our_region_tel"


async def process_rag_search(
    message: str,
    reformed_query: str,
    temperature: float,
    max_tokens: int,
    stream: bool,
    frequency_penalty: float,
    repetition_penalty: float,
    top_p: float,
    top_k: int,
    seed: int,
    tools: list,
    status_callback: callable = None,
    intent: str = "search",
    messages: list = None,
    sigun_filters: list = None,
    precomputed_expanded_queries: list = None,
    precomputed_keywords: list = None,
    excluded_chunk_ids: List[str] = None,
    excluded_service_names: List[str] = None,
    final_user_message: Optional[str] = None,
    precomputed_search_target: Optional[str] = None,
) -> tuple[Any, List[Dict[str, str]]]:
    """
    RAG 문서 검색 및 최종 응답 생성 — search 전용

    `precomputed_search_target`: admin_local_office → OUR_REGION_TEL 만,
    welfare_facility → WELFARE_CENTER 만 Mariner 검색. ambiguous·미전달은 OUR_REGION_TEL.
    """

    try:
        t_total = time.monotonic()
        search_pool_tag = _search_pool_tag_from_target(precomputed_search_target)
        logger.info(
            "[RAG/search_v2] 전처리 search_target=%s → 단일 블록 %s",
            precomputed_search_target or "(미전달·ambiguous 처리)",
            search_pool_tag,
        )
        _skip_policy_boost = bool(excluded_chunk_ids or excluded_service_names)

        with log_step_banner(logger, "RAG/search_v2 Step1 쿼리 확장"):
            if precomputed_expanded_queries:
                expanded_queries = dedupe_cap_expanded_queries(
                    precomputed_expanded_queries, reformed_query=reformed_query
                )
                logger.info(
                    "[RAG/search_v2] 사전 계산된 확장 쿼리 사용: 원본 %d개 → 캡 후 %d개",
                    len(precomputed_expanded_queries or []),
                    len(expanded_queries),
                )
            else:
                if status_callback:
                    await status_callback("최적의 답변방식을 찾고 있습니다")
                _t = time.monotonic()
                expanded_queries = await expand_query(reformed_query)
                logger.info("[TIMING][search] Step1 쿼리 확장: %.3fs", time.monotonic() - _t)
                if not expanded_queries:
                    logger.warning("[RAG/search_v2] 쿼리 확장 실패 - 원본 질의 사용")
                    expanded_queries = [reformed_query]
            if not expanded_queries:
                expanded_queries = [reformed_query]
            logger.info(f"[RAG/search_v2] 확장 완료: {len(expanded_queries)}개 쿼리")
            for i, eq in enumerate(expanded_queries, 1):
                logger.debug(f"[RAG/search_v2] [벡터검색어] #{i}: {eq}")

        with log_step_banner(logger, "RAG/search_v2 Step2 트리플 추출"):
            if precomputed_keywords:
                keyword_str = " ".join(precomputed_keywords)
                search_queries = [keyword_str] if keyword_str.strip() else []
                logger.info("[RAG/search_v2] 사전 계산된 키워드 사용: %d개", len(precomputed_keywords or []))
                logger.debug("[RAG/search_v2] 사전 계산된 키워드 상세: %s", precomputed_keywords)
            else:
                if status_callback:
                    await status_callback("내용을 정리하고 있습니다")
                _t = time.monotonic()
                triples_list = await asyncio.gather(
                    *[extract_triples(eq) for eq in expanded_queries]
                )
                search_queries = _build_search_queries(list(triples_list))
                logger.info("[TIMING][search] Step2 트리플 추출: %.3fs", time.monotonic() - _t)
                logger.info(
                    f"[RAG/search_v2] 키워드 추출 완료: "
                    f"검색쿼리 {len(search_queries)}개 (총 {len(triples_list)}개 중)"
                )

        logger.debug("-----------[RAG/search_v2 Step3 SIGUN 필터 시작]-----------")
        search_sigun_filters = sigun_filters or []
        logger.debug(f"[RAG/search_v2] sigun 필터: {search_sigun_filters}")
        logger.debug("-----------[RAG/search_v2 Step3 SIGUN 필터 끝]-----------")

        with log_step_banner(logger, "RAG/search_v2 Step4 Mariner 단일 풀 검색"):
            _SEARCH_PER_QUERY = -1
            if status_callback:
                await status_callback("시설 및 문의처를 검색하고 있습니다")

            for i, sq in enumerate(search_queries, 1):
                logger.debug(f"[RAG/search_v2] [키워드검색어] #{i}: {sq}")
            all_queries, _welfare_policy_qs = build_welfare_search_queries(
                expanded_queries=expanded_queries,
                search_queries=search_queries,
                user_message=message,
                policy_search_boost_enabled=not _skip_policy_boost,
            )
            if _welfare_policy_qs:
                logger.debug("[RAG/search_v2] 정책 보강 검색어: %s", _welfare_policy_qs)

        # ---- WELFARE_CENTER(GSND_WELFARE_CENTER) 검색 헬퍼 (임시 비활성화; 복구: 이 블록·상단 import) ----
        def _run_welfare_center_query(_query: str) -> List[Dict[str, Any]]:
            return []

        from app.chat.sigun import extract_eupmyeondong_from_message

        _search_eupmyeondong = extract_eupmyeondong_from_message(message)
        _search_eupmyeondong_filters = [_search_eupmyeondong] if _search_eupmyeondong else []

        def _run_welfare_tel_query(query: str):
            try:
                return query_welfare_tel_documents(
                    query,
                    sigun_filters=search_sigun_filters,
                    eupmyeondong_filters=_search_eupmyeondong_filters,
                    max_results=-1,
                )
            except Exception as e:
                logger.warning(f"[RAG/search_v2] OUR_REGION_TEL 검색 실패: {e}")
                return []

        _tel_only = search_pool_tag == "our_region_tel"
        _center_only = search_pool_tag == "welfare_center"

        _t = time.monotonic()
        center_docs, tel_docs = await collect_welfare_center_tel_docs(
            queries=all_queries,
            per_query_limit=_SEARCH_PER_QUERY,
            per_query_limit_tel=-1,
            run_center_query=_run_welfare_center_query,
            run_tel_query=_run_welfare_tel_query,
            log_prefix="RAG/search_v2",
            center_enabled=_center_only,
            tel_enabled=_tel_only,
        )
        logger.info("[TIMING][search] Step4 Mariner 단일 풀: %.3fs", time.monotonic() - _t)
        if _tel_only:
            all_docs = tel_docs
            logger.info(f"[RAG/search_v2] OUR_REGION_TEL 전용 검색 완료: {len(all_docs)}건")
        else:
            all_docs = center_docs
            logger.info(f"[RAG/search_v2] WELFARE_CENTER 전용 검색 완료: {len(all_docs)}건")

        logger.debug("-----------[RAG/search_v2 Step5 top_docs 확정 시작]-----------")
        top_docs = sorted(
            _deduplicate_documents(all_docs),
            key=lambda x: float(x.get("WEIGHT", 0) or 0),
            reverse=True,
        )
        logger.info(f"[RAG/search_v2] 최종 선택: {len(top_docs)}개 (총 {len(all_docs)}개 수집)")
        for i, doc in enumerate(top_docs, 1):
            source = doc.get("_source", "center")
            logger.debug(
                f"[RAG/search_v2] #{i} [{source}] NAME={_get_document_name(doc) or '?'}, "
                f"WEIGHT={doc.get('WEIGHT', '?')}"
            )
        logger.debug("-----------[RAG/search_v2 Step5 top_docs 확정 끝]-----------")

        logger.debug("-----------[RAG/search_v2 Step6 관련성 필터 시작]-----------")
        if status_callback:
            await status_callback("검색 결과를 검증하고 있습니다")
        _t_ref = time.monotonic()
        top_docs = apply_policy_priority_to_documents(
            message,
            top_docs,
            log_prefix="[RAG/search_v2]",
            apply_enabled=not _skip_policy_boost,
        )
        if search_pool_tag == "our_region_tel":
            logger.info(
                "[RAG/search_v2] 단일 블록 OUR_REGION_TEL — 관련성 LLM 필터 생략 (%d건 유지)",
                len(top_docs),
            )
            logger.info("[TIMING][search] Step6 관련성 필터: 생략 (0s)")
        else:
            top_docs = await filter_irrelevant_docs(
                reformed_query,
                top_docs,
                sigun_filters=search_sigun_filters,
                max_judgment_docs=-1,
            )
            logger.info("[TIMING][search] Step6 관련성 필터 [8b/sllm]: %.3fs", time.monotonic() - _t_ref)
            logger.info(f"[RAG/search_v2] 관련성 필터 후: {len(top_docs)}개 문서")
        logger.debug("-----------[RAG/search_v2 Step6 관련성 필터 끝]-----------")

        if excluded_chunk_ids or excluded_service_names:
            from .common import filter_excluded_docs
            top_docs = filter_excluded_docs(top_docs, excluded_chunk_ids or [], excluded_service_names)
            logger.info(f"[MoreResults][search] 제외 필터 후: {len(top_docs)}개 문서")

        logger.debug("-----------[RAG/search_v2 Step7 최종응답 생성 시작]-----------")
        if status_callback:
            await status_callback("최종답변을 생성하고 있습니다")
        _t = time.monotonic()
        _final_user_msg = (final_user_message or "").strip() or message
        response = await generate_final_response_v2(
            _final_user_msg, top_docs, temperature, max_tokens, stream,
            frequency_penalty, repetition_penalty, top_p, top_k, seed, tools,
            intent=intent,
            messages=messages,
            more_info_mode=bool(excluded_chunk_ids or excluded_service_names),
        )
        logger.info("[TIMING][search] Step7 최종 응답 생성 [32b/luxia]: %.3fs", time.monotonic() - _t)
        logger.debug("-----------[RAG/search_v2 Step7 최종응답 생성 끝]-----------")
        logger.info("[TIMING][search] process_rag_search 전체: %.3fs", time.monotonic() - t_total)
        return response, []

    except Exception as e:
        logger.error(f"[RAG/search_v2] 처리 중 오류: {e}", exc_info=True)
        raise
