"""
RAG 서비스 v2 — comparison 플로우

general과 동일한 검색 구조를 사용합니다 (GSND 제외).
OKMS Group A/B 듀얼 검색 → WELFARE_TEL (BUSINESS_NAME 기반) → LLM 응답

기존 services/rag_service.py는 변경하지 않습니다.
"""

import logging
import re
import asyncio
import time
from itertools import zip_longest
from typing import Dict, Any, List, Optional



from core.config import Config

# Mariner 쿼리셋
from mariner_v2.queryset_okms import query_group_a_documents
from mariner_v2.queryset_gov_okms import query_gov_okms_documents
from mariner_v2.queryset_okms_fallback import query_group_a_fallback
from mariner_v2.queryset_welfare_tel import query_welfare_tel_documents

# 복지 문의처 공통 모듈

# 기존 rag_service에서 필요한 함수를 import
from services.rag_service import (
    _deduplicate_documents,
    _get_document_name,
    _get_document_snippet,
    _extract_sigun_from_message,
    _extract_birth_year_from_message,
    _birth_year_to_lifecycle,
    _extract_lifecycle_from_message,
    _build_search_queries,
    filter_okms_keywords,
)
from services_v2.response_generator import generate_final_response_v2
from services.retrieval_judgment_service import retrieval_sufficiency_judgment
from services.router_service import (
    expand_query,
    extract_triples,
)
from mariner.queryset import normalize_sigun
from utils.year_filter import extract_year_filters
from utils.relevance_filter import filter_irrelevant_docs

logger = logging.getLogger(__name__)


async def process_rag_with_documents_v2(
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
    intent: str = "comparison",
    messages: list = None,
    sigun_filters: Optional[List[str]] = None,
    precomputed_expanded_queries: list = None,
    precomputed_keywords: list = None,
    excluded_chunk_ids: List[str] = None,
    excluded_service_names: List[str] = None,
) -> tuple[Any, List[Dict[str, str]]]:
    """
    RAG 문서 검색 및 최종 응답 생성 — comparison 전용

    general과 동일한 검색 구조 (GSND 제외):
    OKMS Group A/B → Fallback → WELFARE_TEL (BUSINESS_NAME) → 관련성 필터 → 응답
    """
    if intent != "comparison":
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
        selected_collection = Config.RAG_OKMS_COLLECTION
        logger.info(f"[RAG/comparison_v2] 컬렉션: {selected_collection}")

        # ====================================================================
        # Step 1: 쿼리 확장 (Mariner 검색 전)
        # ====================================================================
        if precomputed_expanded_queries:
            expanded_queries = precomputed_expanded_queries
            logger.info("[RAG/comparison_v2] 사전 계산된 확장 쿼리 사용: %d개", len(expanded_queries))
        else:
            if status_callback:
                await status_callback("최적의 답변방식을 찾고 있습니다")
            _t = time.monotonic()
            expanded_queries = await expand_query(reformed_query)
            logger.info("[TIMING][comparison] Step1 쿼리 확장: %.3fs", time.monotonic() - _t)
            if not expanded_queries:
                logger.warning("[RAG/comparison_v2] 쿼리 확장 실패 - 원본 질의 사용")
                expanded_queries = [reformed_query]
        logger.info(f"[RAG/comparison_v2] 확장 완료: {len(expanded_queries)}개 쿼리")
        for i, eq in enumerate(expanded_queries, 1):
            logger.info(f"[RAG/comparison_v2] [벡터검색어] #{i}: {eq}")

        # ====================================================================
        # Step 2: 트리플(키워드) 추출 (확장쿼리별)
        # ====================================================================
        if status_callback:
            await status_callback("내용을 정리하고 있습니다")
        _t = time.monotonic()
        if precomputed_keywords:
            triples_list = [filter_okms_keywords(precomputed_keywords)]
            if len(expanded_queries) > 1:
                triples_list.extend([[] for _ in range(len(expanded_queries) - 1)])
            logger.info("[RAG/comparison_v2] 사전 계산된 키워드 사용: %s", precomputed_keywords)
        else:
            triples_list = await asyncio.gather(*[extract_triples(eq) for eq in expanded_queries])
            triples_list = [filter_okms_keywords(kws) for kws in triples_list]
        search_queries = _build_search_queries(list(triples_list))
        logger.info("[TIMING][comparison] Step2 트리플 추출: %.3fs", time.monotonic() - _t)
        logger.info(
            f"[RAG/comparison_v2] 키워드 추출 완료: "
            f"검색쿼리 {len(search_queries)}개 (총 {len(triples_list)}개 중)"
        )
        tri_built = []
        for kws in triples_list:
            sq = " ".join(k.strip() for k in kws if k and k.strip())
            tri_built.append(sq)

        # ====================================================================
        # Step 3: SIGUN/LIFECYCLE/YEAR 추출
        # ====================================================================
        # sigun_filters가 외부에서 전달된 경우(히스토리/위치명 기반 확정값) 그대로 사용
        if sigun_filters is not None:
            comp_sigun_filters = sigun_filters
            logger.info(f"[RAG/comparison_v2] 외부 sigun_filters 사용: {comp_sigun_filters}")
        else:
            comp_sigun_raws = _extract_sigun_from_message(message)
            _comp_normalized = [normalize_sigun(r) for r in comp_sigun_raws if r != "경남"]
            _comp_city_filters = [s for s in _comp_normalized if s.startswith("경상남도 ")]
            comp_sigun_filters = list(dict.fromkeys(_comp_city_filters)) if _comp_city_filters else []
        comp_birth_year = _extract_birth_year_from_message(message)
        comp_lifecycle = (
            _birth_year_to_lifecycle(comp_birth_year)
            if comp_birth_year
            else _extract_lifecycle_from_message(message)
        )
        logger.info(f"[RAG/comparison_v2] 필터 - sigun: {comp_sigun_filters}, lifecycle: '{comp_lifecycle}'")

        comp_year_filters = extract_year_filters(message)
        if comp_year_filters:
            logger.info(f"[RAG/comparison_v2] 연도 필터: {comp_year_filters}")
        else:
            logger.info("[RAG/comparison_v2] 연도 필터 미적용 (기본값: 올해)")

        # ====================================================================
        # OKMS 병렬 검색 헬퍼
        # ====================================================================
        def _group_a_run(vector: str, keywords: str):
            try:
                return query_group_a_documents(
                    vector, keywords, selected_collection,
                    year_filters=comp_year_filters or None,
                    sigun_filters=comp_sigun_filters,
                    lifecycle_filter=comp_lifecycle or None,
                    excluded_chunk_ids=excluded_chunk_ids,
                )
            except Exception as e:
                logger.warning(f"[RAG/comparison_v2] Group A 검색 실패: {e}")
                return [], []

        def _group_a_fallback_run(vector: str, keywords: str):
            try:
                return query_group_a_fallback(
                    vector, keywords, selected_collection,
                    sigun_filters=comp_sigun_filters,
                    excluded_chunk_ids=excluded_chunk_ids,
                )
            except Exception as e:
                logger.warning(f"[RAG/comparison_v2] Group A Fallback 실패: {e}")
                return [], []

        def _run_gov_okms_query(search_str: str):
            """GOV_OKMS_V1 단일 검색: SIGUN/YEAR 필터 없음, LIFE_CYCLE만 선택 적용"""
            try:
                return query_gov_okms_documents(
                    search_str,
                    collection=Config.RAG_GOV_OKMS_COLLECTION,
                    lifecycle_filter=comp_lifecycle or None,
                    sigun_filters=comp_sigun_filters,
                    excluded_chunk_ids=excluded_chunk_ids,
                )
            except Exception as e:
                logger.warning(f"[RAG/comparison_v2] GOV_OKMS 쿼리 실패: {e}")
                return []

        loop = asyncio.get_event_loop()

        for i, sq in enumerate(tri_built, 1):
            if sq:
                logger.info(f"[RAG/comparison_v2] [키워드검색어] #{i}: {sq}")

        # ====================================================================
        # Step 4-A: OKMS Group A + GOV_OKMS 단일 검색 (동시 병렬)
        # ====================================================================
        _COMP_GA_PER_QUERY = 5
        _COMP_GA_TOP_N = 10
        if status_callback:
            await status_callback("질문을 분석하고 있습니다")

        ga_pair_futures = [
            loop.run_in_executor(None, _group_a_run, eq, sq if sq else "")
            for eq, sq in zip_longest(expanded_queries, tri_built, fillvalue="")
        ]
        gov_okms_futures = [
            loop.run_in_executor(None, _run_gov_okms_query, s)
            for s in list(expanded_queries) + [sq for sq in tri_built if sq]
        ]
        if status_callback:
            await status_callback("문서를 검색하고 있습니다")
        _t = time.monotonic()
        all_results = await asyncio.gather(*ga_pair_futures, *gov_okms_futures)
        logger.info("[TIMING][comparison] Step4-A OKMS GroupA+GOV_OKMS 병렬 검색: %.3fs", time.monotonic() - _t)
        ga_pair_results = all_results[:len(ga_pair_futures)]
        gov_okms_results = all_results[len(ga_pair_futures):]

        ga_vector_results = [pair[1] for pair in ga_pair_results]
        ga_keyword_results = [pair[0] for pair in ga_pair_results]

        comp_group_a_docs: List[Dict[str, Any]] = []
        for i, docs in enumerate(ga_vector_results, 1):
            if docs:
                comp_group_a_docs.extend(docs[:_COMP_GA_PER_QUERY])
                logger.info(f"[RAG/comparison_v2] [GroupA] 확장쿼리 #{i}: {min(len(docs), _COMP_GA_PER_QUERY)}개")
        for i, docs in enumerate(ga_keyword_results, 1):
            if not tri_built[i - 1]:
                continue
            if docs:
                comp_group_a_docs.extend(docs[:_COMP_GA_PER_QUERY])
                logger.info(f"[RAG/comparison_v2] [GroupA] 트리플쿼리 #{i}: {min(len(docs), _COMP_GA_PER_QUERY)}개")

        gov_okms_docs: List[Dict[str, Any]] = []
        for i, docs in enumerate(gov_okms_results, 1):
            if docs:
                gov_okms_docs.extend(docs[:_COMP_GA_PER_QUERY])
                logger.info(f"[RAG/comparison_v2] [GOV_OKMS] #{i}: {min(len(docs), _COMP_GA_PER_QUERY)}개")
            else:
                logger.info(f"[RAG/comparison_v2] [GOV_OKMS] #{i}: 0개")

        comp_group_a_top = sorted(
            _deduplicate_documents(comp_group_a_docs + gov_okms_docs),
            key=lambda x: float(x.get("WEIGHT", 0) or 0),
            reverse=True,
        )[:_COMP_GA_TOP_N]
        logger.info(
            f"[RAG/comparison_v2] [GroupA+GOV_OKMS] 최종: {len(comp_group_a_top)}개 "
            f"(OKMS {len(comp_group_a_docs)}개 + GOV_OKMS {len(gov_okms_docs)}개 수집)"
        )
        for idx, doc in enumerate(comp_group_a_top, 1):
            logger.info(
                f"[RAG/comparison_v2] [GroupA] #{idx}"
                f"  NAME={doc.get('NAME', '')}"
                f"  WEIGHT={doc.get('WEIGHT', '')}"
                f"  YEAR={doc.get('YEAR', '')}"
                f"  SIGUN={doc.get('SIGUN', '')}"
                f"  CHUNK_ID={doc.get('CHUNK_ID', '')}"
            )

        # ====================================================================
        # Step 5: Group A → top 5
        # ====================================================================
        _COMP_FINAL_TOP_N = 8
        _FALLBACK_THRESHOLD = 1
        okms_final = comp_group_a_top[:_COMP_FINAL_TOP_N]
        logger.info(f"[RAG/comparison_v2] OKMS 최종: {len(okms_final)}개 (GroupA {len(comp_group_a_top)}개)")

        # ====================================================================
        # Step 5-F: OKMS Fallback (< 3건
        # ====================================================================
        if len(okms_final) < _FALLBACK_THRESHOLD:
            logger.info(f"[RAG/comparison_v2] OKMS {len(okms_final)}건 < {_FALLBACK_THRESHOLD}건 → Fallback 시작")

            ga_fb_futures = [
                loop.run_in_executor(None, _group_a_fallback_run, eq, sq if sq else "")
                for eq, sq in zip_longest(expanded_queries, tri_built, fillvalue="")
            ]
            _t = time.monotonic()
            ga_fb_results = await asyncio.gather(*ga_fb_futures)
            logger.info("[TIMING][comparison] Step5-F OKMS Fallback 검색(병렬): %.3fs", time.monotonic() - _t)

            fb_a_docs: List[Dict[str, Any]] = []
            for i, pair in enumerate(ga_fb_results, 1):
                if pair[1]:
                    fb_a_docs.extend(pair[1][:_COMP_GA_PER_QUERY])
                if tri_built[i - 1] and pair[0]:
                    fb_a_docs.extend(pair[0][:_COMP_GA_PER_QUERY])

            okms_final = sorted(
                _deduplicate_documents(okms_final + fb_a_docs),
                key=lambda x: float(x.get("WEIGHT", 0) or 0),
                reverse=True,
            )[:_COMP_FINAL_TOP_N]
            logger.info(f"[RAG/comparison_v2] Fallback 후: {len(okms_final)}개 (FB-A {len(fb_a_docs)}개)")

        # ====================================================================
        # Step 5-S: OKMS → WELFARE_TEL 전환 적합성 판단 (LLM)
        # ====================================================================
        _t = time.monotonic()
        okms_sufficiency = await retrieval_sufficiency_judgment(
            user_question=message,
            intent=intent,
            collection_name=Config.RAG_OKMS_COLLECTION,
            docs=okms_final,
        )
        logger.info("[TIMING][comparison] Step5-S OKMS 적합성 판단 [8b/sllm]: %.3fs", time.monotonic() - _t)
        logger.info(
            f"[RAG/comparison_v2] OKMS 적합성: sufficient={okms_sufficiency['sufficient']}, "
            f"reason={okms_sufficiency['reason']}"
        )

        # ====================================================================
        # Step 6: WELFARE_TEL 검색 (OKMS BUSINESS_NAME + SIGUN 기반, OKMS 부족 시)
        # ====================================================================
        _COMP_WELFARE_TEL_PER_QUERY = 5
        welfare_tel_docs: List[Dict[str, Any]] = []

        if okms_sufficiency["sufficient"]:
            logger.info("[RAG/comparison_v2] OKMS 결과 충분 — OUR_REGION_TEL 검색 생략")
        else:
            # 2차 fallback: 확장 쿼리 기반 OKMS 보강 검색
            if status_callback:
                await status_callback("검색을 보강하고 있습니다")
            _t = time.monotonic()
            if precomputed_expanded_queries:
                _candidates = precomputed_expanded_queries
                logger.info("[RAG/comparison_v2] fallback: 사전 계산 확장 쿼리 사용")
            else:
                _candidates = await expand_query(reformed_query)
            logger.info("[TIMING][comparison] Step5-S2 fallback 쿼리확장: %.3fs", time.monotonic() - _t)
            fallback_expanded_queries = [
                q for q in (str(x).strip() for x in (_candidates or []))
                if q and q != reformed_query
            ]
            if fallback_expanded_queries:
                _t = time.monotonic()
                fallback_triples = await asyncio.gather(*[extract_triples(eq) for eq in fallback_expanded_queries])
                fallback_tri_built = [
                    " ".join(k.strip() for k in filter_okms_keywords(kws) if k and k.strip())
                    for kws in fallback_triples
                ]
                fb_pair_futures = [
                    loop.run_in_executor(None, _group_a_run, eq, sq if sq else "")
                    for eq, sq in zip_longest(fallback_expanded_queries, fallback_tri_built, fillvalue="")
                ]
                fb_gov_futures = [
                    loop.run_in_executor(None, _run_gov_okms_query, s)
                    for s in list(fallback_expanded_queries) + [sq for sq in fallback_tri_built if sq]
                ]
                fb_results = await asyncio.gather(*fb_pair_futures, *fb_gov_futures)
                logger.info("[TIMING][comparison] Step5-S2 fallback 보강검색: %.3fs", time.monotonic() - _t)
                fb_pair_results = fb_results[:len(fb_pair_futures)]
                fb_gov_results = fb_results[len(fb_pair_futures):]
                fb_docs: List[Dict[str, Any]] = []
                for pair in fb_pair_results:
                    if pair[1]:
                        fb_docs.extend(pair[1][:_COMP_GA_PER_QUERY])
                    if pair[0]:
                        fb_docs.extend(pair[0][:_COMP_GA_PER_QUERY])
                for docs in fb_gov_results:
                    if docs:
                        fb_docs.extend(docs[:_COMP_GA_PER_QUERY])
                if fb_docs:
                    okms_final = sorted(
                        _deduplicate_documents(okms_final + fb_docs),
                        key=lambda x: float(x.get("WEIGHT", 0) or 0),
                        reverse=True,
                    )[:_COMP_FINAL_TOP_N]
                    logger.info("[RAG/comparison_v2] fallback 보강 후 OKMS: %d개", len(okms_final))
                    _t = time.monotonic()
                    okms_sufficiency = await retrieval_sufficiency_judgment(
                        user_question=message,
                        intent=intent,
                        collection_name=Config.RAG_OKMS_COLLECTION,
                        docs=okms_final,
                    )
                    logger.info("[TIMING][comparison] Step5-S3 fallback 재판단: %.3fs", time.monotonic() - _t)
                    logger.info(
                        "[RAG/comparison_v2] fallback 재판단: sufficient=%s, reason=%s",
                        okms_sufficiency["sufficient"],
                        okms_sufficiency["reason"],
                    )

            if okms_sufficiency["sufficient"]:
                logger.info("[RAG/comparison_v2] fallback 후 충분 — OUR_REGION_TEL 검색 생략")
            else:
                logger.info("[RAG/comparison_v2] OKMS 결과 부족 → OUR_REGION_TEL 검색 진행")

                from services.sigun_service import extract_eupmyeondong_from_message
                comp_eupmyeondong = extract_eupmyeondong_from_message(message)
                comp_eupmyeondong_filters = [comp_eupmyeondong] if comp_eupmyeondong else []

                def _run_welfare_tel_query():
                    try:
                        return query_welfare_tel_documents(
                            message,
                            sigun_filters=comp_sigun_filters,
                            eupmyeondong_filters=comp_eupmyeondong_filters,
                            excluded_chunk_ids=excluded_chunk_ids,
                        )
                    except Exception as e:
                        logger.warning(f"[RAG/comparison_v2] OUR_REGION_TEL 검색 실패: {e}")
                        return []

                _t = time.monotonic()
                _tel_result = await loop.run_in_executor(None, _run_welfare_tel_query)
                logger.info("[TIMING][comparison] Step6 OUR_REGION_TEL 검색: %.3fs", time.monotonic() - _t)
                welfare_tel_docs.extend(_tel_result[:_COMP_WELFARE_TEL_PER_QUERY])
                logger.info(f"[RAG/comparison_v2] OUR_REGION_TEL 검색 합계: {len(welfare_tel_docs)}개")

        # OKMS + WELFARE_TEL 합산
        top_docs = sorted(
            _deduplicate_documents(okms_final + welfare_tel_docs),
            key=lambda x: float(x.get("WEIGHT", 0) or 0),
            reverse=True,
        )

        logger.info(f"[RAG/comparison_v2] 최종 선택: {len(top_docs)}개")
        for i, doc in enumerate(top_docs, 1):
            logger.info(f"[RAG/comparison_v2] #{i} NAME={_get_document_name(doc) or '?'}, WEIGHT={doc.get('WEIGHT', '?')}")

        # ====================================================================
        # Step 7: LLM 관련성 필터
        # ====================================================================
        if status_callback:
            await status_callback("검색 결과를 검증하고 있습니다")
        _t = time.monotonic()
        top_docs = await filter_irrelevant_docs(reformed_query, top_docs, sigun_filters=comp_sigun_filters)
        logger.info("[TIMING][comparison] Step7 관련성 필터 [8b/sllm]: %.3fs", time.monotonic() - _t)
        logger.info(f"[RAG/comparison_v2] 관련성 필터 후: {len(top_docs)}개")

        if excluded_chunk_ids or excluded_service_names:
            from services_v2.common import filter_excluded_docs
            top_docs = filter_excluded_docs(top_docs, excluded_chunk_ids or [], excluded_service_names)
            logger.info(f"[MoreResults][comparison] 제외 필터 후: {len(top_docs)}개 문서")

        # Step 7-2: YEAR 최신순 정렬
        top_docs = sorted(
            top_docs,
            key=lambda x: (
                int(re.search(r'(\d{4})', str(x.get("YEAR", "") or "").strip()).group(1))
                if re.search(r'(\d{4})', str(x.get("YEAR", "") or "").strip())
                else 0,
                float(x.get("WEIGHT", 0) or 0),
            ),
            reverse=True,
        )

        # ====================================================================
        # Step 8: 참고 문서 구성
        # ====================================================================
        seen_ids: set = set()
        referenced_documents: List[Dict[str, str]] = []
        for doc in top_docs:
            cid = doc.get("CHUNK_ID", "")
            if cid and cid in seen_ids:
                continue
            if cid:
                seen_ids.add(cid)
            referenced_documents.append({
                "id": str(doc.get("ID", "")),
                "chunk_id": str(cid or ""),
                "name": _get_document_name(doc),
                "snippet": _get_document_snippet(doc),
            })

        # ====================================================================
        # Step 9: 최종 응답 생성
        # ====================================================================
        if status_callback:
            await status_callback("최종답변을 생성하고 있습니다")
        _t = time.monotonic()
        response = await generate_final_response_v2(
            message, top_docs, temperature, max_tokens, stream,
            frequency_penalty, repetition_penalty, top_p, top_k, seed, tools,
            intent="comparison",
            messages=messages,
        )
        logger.info("[TIMING][comparison] Step9 최종 응답 생성 [32b/luxia]: %.3fs", time.monotonic() - _t)
        logger.info("[TIMING][comparison] process_rag_comparison 전체: %.3fs", time.monotonic() - t_total)
        return response, referenced_documents

    except Exception as e:
        logger.error(f"[RAG/comparison_v2] 처리 중 오류: {e}", exc_info=True)
        raise
