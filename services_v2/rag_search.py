"""
RAG 서비스 — search 전용 플로우

search 의도(기관/시설 정보 조회)의 검색 및 응답 생성을 담당합니다.
WELFARE_CENTER + WELFARE_TEL 두 컬렉션을 병렬 검색하여 합산합니다.

기존 services/rag_service.py는 변경하지 않습니다.
"""

import logging
import asyncio
import time
from typing import Dict, Any, List, Optional

from core.config import Config

# Mariner 쿼리셋
from mariner_v2.queryset_welfare import query_welfare_center_documents
from mariner_v2.queryset_welfare_tel import query_welfare_tel_documents

# 기존 rag_service에서 필요한 함수를 import
from services.rag_service import (
    _deduplicate_documents,
    _get_document_name,
    _build_search_queries,
)
from services_v2.common import build_referenced_documents
from services_v2.response_generator import generate_final_response_v2
from services.retrieval_judgment_service import retrieval_sufficiency_judgment
from services.router_service import (
    expand_query,
    extract_triples,
)
from utils.relevance_filter import filter_irrelevant_docs

logger = logging.getLogger(__name__)


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
) -> tuple[Any, List[Dict[str, str]]]:
    """
    RAG 문서 검색 및 최종 응답 생성 — search 전용

    WELFARE_CENTER(시설 정보)와 WELFARE_TEL(문의처 연락처)을
    병렬 검색하여 합산 후 관련성 필터를 거쳐 응답합니다.
    """
    if intent != "search":
        from services.rag_service import process_rag_with_documents
        return await process_rag_with_documents(
            message=message,
            reformed_query=reformed_query,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=stream,
            frequency_penalty=frequency_penalty,
            repetition_penalty=repetition_penalty,
            top_p=top_p,
            top_k=top_k,
            seed=seed,
            tools=tools,
            status_callback=status_callback,
            intent=intent,
            messages=messages,
        )

    try:
        t_total = time.monotonic()

        # ====================================================================
        # Step 1: 쿼리 확장 (Mariner 검색 전)
        # ====================================================================
        if precomputed_expanded_queries:
            expanded_queries = precomputed_expanded_queries
            logger.info("[RAG/search_v2] 사전 계산된 확장 쿼리 사용: %d개", len(expanded_queries))
        else:
            if status_callback:
                await status_callback("최적의 답변방식을 찾고 있습니다")
            _t = time.monotonic()
            expanded_queries = await expand_query(reformed_query)
            logger.info("[TIMING][search] Step1 쿼리 확장: %.3fs", time.monotonic() - _t)
            if not expanded_queries:
                logger.warning("[RAG/search_v2] 쿼리 확장 실패 - 원본 질의 사용")
                expanded_queries = [reformed_query]
        logger.info(f"[RAG/search_v2] 확장 완료: {len(expanded_queries)}개 쿼리")
        for i, eq in enumerate(expanded_queries, 1):
            logger.info(f"[RAG/search_v2] [벡터검색어] #{i}: {eq}")

        # ====================================================================
        # Step 2: 트리플(키워드) 추출 (확장쿼리별)
        # ====================================================================
        if precomputed_keywords:
            keyword_str = " ".join(precomputed_keywords)
            search_queries = [keyword_str] if keyword_str.strip() else []
            logger.info("[RAG/search_v2] 사전 계산된 키워드 사용: %s", precomputed_keywords)
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

        # ====================================================================
        # Step 3: SIGUN 필터 (chat.py에서 전달)
        # ====================================================================
        search_sigun_filters = sigun_filters or []
        logger.info(f"[RAG/search_v2] sigun 필터: {search_sigun_filters}")

        # ====================================================================
        # Step 3-S: 특정 시설명 감지 → DB/Mariner 직접 조회 (이름 검색 우선 경로)
        # ====================================================================
        from services.rag_service.facility import _extract_specific_facility_name
        from services.rag_service.welfare_search import _lookup_facility_by_name

        _specific_facility = _extract_specific_facility_name(message)
        if _specific_facility:
            logger.info(f"[RAG/search_v2] 시설명 직접 조회: '{_specific_facility}'")
            name_docs = await asyncio.get_event_loop().run_in_executor(
                None, _lookup_facility_by_name, _specific_facility
            )
            if name_docs:
                logger.info(f"[RAG/search_v2] 시설명 직접 조회 결과: {len(name_docs)}개 → 바로 응답")
                top_docs = name_docs[:10]
                referenced_documents = build_referenced_documents(top_docs)
                _final_user_msg = (final_user_message or "").strip() or message
                response = await generate_final_response_v2(
                    _final_user_msg, top_docs, temperature, max_tokens, stream,
                    frequency_penalty, repetition_penalty, top_p, top_k, seed, tools,
                    intent=intent, messages=messages,
                )
                logger.info("[TIMING][search] process_rag_search 전체: %.3fs", time.monotonic() - t_total)
                return response, referenced_documents[:Config.RAG_UI_MAX_DOCS]

        # ====================================================================
        # Step 4: WELFARE_CENTER + WELFARE_TEL 병렬 검색
        # ====================================================================
        _SEARCH_PER_QUERY = 5
        _SEARCH_TOP_N = 10
        if status_callback:
            await status_callback("시설 및 문의처를 검색하고 있습니다")

        for i, sq in enumerate(search_queries, 1):
            logger.info(f"[RAG/search_v2] [키워드검색어] #{i}: {sq}")
        all_queries = list(expanded_queries) + list(search_queries)
        loop = asyncio.get_event_loop()

        # ---- WELFARE_CENTER 검색 헬퍼 ----
        def _run_welfare_center_query(query: str):
            try:
                return query_welfare_center_documents(
                    query,
                    sigun_filters=search_sigun_filters,
                    excluded_chunk_ids=excluded_chunk_ids,
                )
            except Exception as e:
                logger.warning(f"[RAG/search_v2] WELFARE_CENTER 검색 실패: {e}")
                return []

        # ---- OUR_REGION_TEL 검색 헬퍼 ----
        from services.sigun_service import extract_eupmyeondong_from_message
        _search_eupmyeondong = extract_eupmyeondong_from_message(message)
        _search_eupmyeondong_filters = [_search_eupmyeondong] if _search_eupmyeondong else []

        def _run_welfare_tel_query(query: str):
            try:
                return query_welfare_tel_documents(
                    query,
                    sigun_filters=search_sigun_filters,
                    eupmyeondong_filters=_search_eupmyeondong_filters,
                    excluded_chunk_ids=excluded_chunk_ids,
                )
            except Exception as e:
                logger.warning(f"[RAG/search_v2] OUR_REGION_TEL 검색 실패: {e}")
                return []

        # 두 컬렉션 × 모든 쿼리 병렬 실행
        center_futures = [
            loop.run_in_executor(None, _run_welfare_center_query, q)
            for q in all_queries
        ]
        tel_futures = [
            loop.run_in_executor(None, _run_welfare_tel_query, q)
            for q in all_queries
        ]
        _t = time.monotonic()
        center_results, tel_results = await asyncio.gather(
            asyncio.gather(*center_futures),
            asyncio.gather(*tel_futures),
        )
        logger.info("[TIMING][search] Step4 CENTER+TEL 병렬 검색: %.3fs", time.monotonic() - _t)

        # WELFARE_CENTER 결과 수집
        center_docs: List[Dict[str, Any]] = []
        for i, (q, docs) in enumerate(zip(all_queries, center_results), 1):
            if docs:
                center_docs.extend(docs[:_SEARCH_PER_QUERY])
                logger.info(f"[RAG/search_v2] [CENTER] 쿼리 #{i} '{q[:30]}': {min(len(docs), _SEARCH_PER_QUERY)}개")
            else:
                logger.info(f"[RAG/search_v2] [CENTER] 쿼리 #{i} '{q[:30]}': 0개")

        # WELFARE_TEL 결과 수집
        tel_docs: List[Dict[str, Any]] = []
        for i, (q, docs) in enumerate(zip(all_queries, tel_results), 1):
            if docs:
                tel_docs.extend(docs[:_SEARCH_PER_QUERY])
                logger.info(f"[RAG/search_v2] [TEL] 쿼리 #{i} '{q[:30]}': {min(len(docs), _SEARCH_PER_QUERY)}개")
            else:
                logger.info(f"[RAG/search_v2] [TEL] 쿼리 #{i} '{q[:30]}': 0개")

        logger.info(
            f"[RAG/search_v2] 검색 합계: CENTER {len(center_docs)}개 + TEL {len(tel_docs)}개"
        )

        # ====================================================================
        # Step 4-S: WELFARE_TEL → WELFARE_CENTER 전환 적합성 판단 (LLM)
        # ====================================================================
        tel_top_for_judgment = sorted(
            _deduplicate_documents(tel_docs),
            key=lambda x: float(x.get("WEIGHT", 0) or 0),
            reverse=True,
        )[:_SEARCH_TOP_N]

        _t = time.monotonic()
        tel_sufficiency = await retrieval_sufficiency_judgment(
            user_question=message,
            intent=intent,
            collection_name=Config.RAG_WELFARE_TEL_COLLECTION,
            docs=tel_top_for_judgment,
        )
        logger.info("[TIMING][search] Step4-S OUR_REGION_TEL 적합성 판단 [8b/sllm]: %.3fs", time.monotonic() - _t)
        logger.info(
            f"[RAG/search_v2] OUR_REGION_TEL 적합성: sufficient={tel_sufficiency['sufficient']}, "
            f"reason={tel_sufficiency['reason']}"
        )

        if tel_sufficiency["sufficient"] and tel_sufficiency.get("reason") != "judgment_disabled":
            logger.info("[RAG/search_v2] OUR_REGION_TEL 결과 충분 — WELFARE_CENTER 결과 제외")
            all_docs = tel_docs
        else:
            # 2차 fallback: 확장 쿼리 기반 보강 검색
            fallback_center_docs: List[Dict[str, Any]] = []
            fallback_tel_docs: List[Dict[str, Any]] = []
            _t = time.monotonic()
            if precomputed_expanded_queries:
                _candidates = precomputed_expanded_queries
                logger.info("[RAG/search_v2] fallback: 사전 계산 확장 쿼리 사용")
            else:
                _candidates = await expand_query(reformed_query)
            fallback_expanded = [
                q for q in (str(x).strip() for x in (_candidates or []))
                if q and q != reformed_query
            ]
            logger.info("[TIMING][search] Step4-S2 fallback 쿼리확장: %.3fs", time.monotonic() - _t)
            if fallback_expanded:
                _t = time.monotonic()
                fb_center_futures = [
                    loop.run_in_executor(None, _run_welfare_center_query, q)
                    for q in fallback_expanded
                ]
                fb_tel_futures = [
                    loop.run_in_executor(None, _run_welfare_tel_query, q)
                    for q in fallback_expanded
                ]
                fb_center_results, fb_tel_results = await asyncio.gather(
                    asyncio.gather(*fb_center_futures),
                    asyncio.gather(*fb_tel_futures),
                )
                logger.info("[TIMING][search] Step4-S2 fallback 보강검색: %.3fs", time.monotonic() - _t)
                for docs in fb_center_results:
                    if docs:
                        fallback_center_docs.extend(docs[:_SEARCH_PER_QUERY])
                for docs in fb_tel_results:
                    if docs:
                        fallback_tel_docs.extend(docs[:_SEARCH_PER_QUERY])
                logger.info(
                    "[RAG/search_v2] fallback 보강: CENTER %d개 + TEL %d개",
                    len(fallback_center_docs),
                    len(fallback_tel_docs),
                )
            center_docs.extend(fallback_center_docs)
            tel_docs.extend(fallback_tel_docs)
            if tel_sufficiency.get("reason") == "judgment_disabled":
                logger.info("[RAG/search_v2] 판단 비활성화 — OUR_REGION_TEL + WELFARE_CENTER 결과 합산")
            else:
                logger.info("[RAG/search_v2] OUR_REGION_TEL 결과 부족 — WELFARE_CENTER 결과 포함")
            all_docs = center_docs + tel_docs

        # ====================================================================
        # Step 5: 합산 → 중복 제거 → WEIGHT top N
        # ====================================================================
        top_docs = sorted(
            _deduplicate_documents(all_docs),
            key=lambda x: float(x.get("WEIGHT", 0) or 0),
            reverse=True,
        )[:_SEARCH_TOP_N]
        logger.info(f"[RAG/search_v2] 최종 선택: {len(top_docs)}개 (총 {len(all_docs)}개 수집)")
        for i, doc in enumerate(top_docs, 1):
            source = doc.get("_source", "center")
            logger.info(f"[RAG/search_v2] #{i} [{source}] NAME={_get_document_name(doc) or '?'}, WEIGHT={doc.get('WEIGHT', '?')}")

        # ====================================================================
        # Step 6: LLM 관련성 필터 (무관 문서 제거)
        # ====================================================================
        if status_callback:
            await status_callback("검색 결과를 검증하고 있습니다")
        _t = time.monotonic()
        top_docs = await filter_irrelevant_docs(reformed_query, top_docs, sigun_filters=search_sigun_filters)
        logger.info("[TIMING][search] Step6 관련성 필터 [8b/sllm]: %.3fs", time.monotonic() - _t)
        logger.info(f"[RAG/search_v2] 관련성 필터 후: {len(top_docs)}개 문서")

        if excluded_chunk_ids or excluded_service_names:
            from services_v2.common import filter_excluded_docs
            top_docs = filter_excluded_docs(top_docs, excluded_chunk_ids or [], excluded_service_names)
            logger.info(f"[MoreResults][search] 제외 필터 후: {len(top_docs)}개 문서")

        # ====================================================================
        # Step 7: 참고 문서 구성
        # ====================================================================
        referenced_documents = build_referenced_documents(top_docs)

        # ====================================================================
        # Step 8: 최종 응답 생성
        # ====================================================================
        if status_callback:
            await status_callback("최종답변을 생성하고 있습니다")
        _t = time.monotonic()
        _final_user_msg = (final_user_message or "").strip() or message
        response = await generate_final_response_v2(
            _final_user_msg, top_docs, temperature, max_tokens, stream,
            frequency_penalty, repetition_penalty, top_p, top_k, seed, tools,
            intent=intent,
            messages=messages,
        )
        logger.info("[TIMING][search] Step8 최종 응답 생성 [32b/luxia]: %.3fs", time.monotonic() - _t)
        logger.info("[TIMING][search] process_rag_search 전체: %.3fs", time.monotonic() - t_total)
        return response, referenced_documents

    except Exception as e:
        logger.error(f"[RAG/search_v2] 처리 중 오류: {e}", exc_info=True)
        raise
