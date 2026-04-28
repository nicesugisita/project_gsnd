"""
RAG 서비스 — general 전용 플로우

general 의도의 검색 및 응답 생성을 별도 파일로 분리한 것입니다.
로직 자체는 기존 services/rag_service.py의 general 플로우와 동일합니다.

기존 services/rag_service.py는 변경하지 않습니다.
"""

import logging
import asyncio
import time
from typing import Dict, Any, List, Optional

from app.core.config import Config

# general 전용 Mariner 쿼리셋
from app.mariner.queryset_okms import query_group_a_documents
from app.mariner.queryset_gov_okms import query_gov_okms_documents
from app.mariner.queryset_okms_fallback import query_group_a_fallback
from app.mariner.queryset_gsnd import query_GSND_general_documents
from .common import (
    sort_weight_top30_then_year,
    build_referenced_documents,
    run_welfare_tel_queries,
    filter_excluded_docs,
)

# 기존 rag_service에서 필요한 함수/상수를 import
from app.chat.infra.rag import (
    _deduplicate_documents,
    _get_document_name,
    _extract_sigun_from_message,
    _extract_birth_year_from_message,
    _birth_year_to_lifecycle,
    _extract_lifecycle_from_message,
    _build_search_queries,
    _resolve_collection_for_intent,
    filter_okms_keywords,
)
from .response_generator import generate_final_response_v2
from app.chat.retrieval_judgment import retrieval_sufficiency_judgment
from app.chat.routing import (
    select_collection_category,
    expand_query,
    extract_triples,
)
from app.mariner.sigun_utils import normalize_sigun
from app.shared.utils.year_filter import extract_year_filters
from app.shared.utils.relevance_filter import filter_irrelevant_docs

logger = logging.getLogger(__name__)


async def process_rag_general(
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
    intent: str = "general",
    messages: list = None,
    sigun_filters: Optional[List[str]] = None,
    precomputed_expanded_queries: list = None,
    precomputed_keywords: list = None,
    excluded_chunk_ids: List[str] = None,
    excluded_service_names: List[str] = None,
    final_user_message: Optional[str] = None,
) -> tuple[Any, List[Dict[str, str]]]:
    """
    RAG 문서 검색 및 최종 응답 생성 — general 전용

    general 의도일 때만 이 함수의 로직을 실행하며,
    그 외 의도는 기존 함수로 위임합니다.
    """

    try:
        t_total = time.monotonic()

        # Step 1: 컬렉션 선택
        _t = time.monotonic()
        base_collection = await select_collection_category(reformed_query)
        selected_collection = _resolve_collection_for_intent(intent, base_collection)
        logger.info(f"[RAG/general_v2] 선택된 컬렉션: {selected_collection}")
        logger.info("[TIMING][general] Step1 컬렉션 선택: %.3fs", time.monotonic() - _t)

        # Step 2: 쿼리 확장 (Mariner 검색 전)
        if precomputed_expanded_queries:
            expanded_queries = precomputed_expanded_queries
            logger.info("[RAG/general_v2] 사전 계산된 확장 쿼리 사용: %d개", len(expanded_queries))
        else:
            if status_callback:
                await status_callback("최적의 답변방식을 찾고 있습니다")
            _t = time.monotonic()
            expanded_queries = await expand_query(reformed_query)
            logger.info("[TIMING][general] Step2 쿼리 확장: %.3fs", time.monotonic() - _t)
            if not expanded_queries:
                logger.warning("[RAG/general_v2] 쿼리 확장 실패 - 원본 질의 사용")
                expanded_queries = [reformed_query]
        logger.info(f"[RAG/general_v2] 확장 완료: {len(expanded_queries)}개 쿼리")
        for i, eq in enumerate(expanded_queries, 1):
            logger.info(f"[RAG/general_v2] [벡터검색어] #{i}: {eq}")

        # Step 3: 트리플(키워드) 추출 (확장쿼리별)
        if status_callback:
            await status_callback("내용을 정리하고 있습니다")
        _t = time.monotonic()
        if precomputed_keywords:
            triples_list = [filter_okms_keywords(precomputed_keywords)]
            if len(expanded_queries) > 1:
                triples_list.extend([[] for _ in range(len(expanded_queries) - 1)])
            logger.info("[RAG/general_v2] 사전 계산된 키워드 사용: %s", precomputed_keywords)
        else:
            triples_list = await asyncio.gather(*[extract_triples(eq) for eq in expanded_queries])
            triples_list = [filter_okms_keywords(kws) for kws in triples_list]
        search_queries = _build_search_queries(list(triples_list))
        logger.info("[TIMING][general] Step3 트리플 추출: %.3fs", time.monotonic() - _t)
        logger.info(
            f"[RAG/general_v2] 키워드 추출 완료: "
            f"검색쿼리 {len(search_queries)}개 (총 {len(triples_list)}개 중)"
        )
        ga_tri_built = []
        for kws in triples_list:
            sq = " ".join(k.strip() for k in kws if k and k.strip())
            ga_tri_built.append(sq)

        # Step 4: SIGUN/LIFECYCLE 추출
        # sigun_filters가 외부에서 전달된 경우(히스토리/위치명 기반 확정값) 그대로 사용
        if sigun_filters is not None:
            gen_sigun_filters = sigun_filters
            logger.info(f"[RAG/general_v2] 외부 sigun_filters 사용: {gen_sigun_filters}")
        else:
            gen_sigun_raws = _extract_sigun_from_message(message)
            _gen_normalized = [normalize_sigun(r) for r in gen_sigun_raws if r != "경남"]
            _gen_city_filters = [s for s in _gen_normalized if s.startswith("경상남도 ")]
            gen_sigun_filters = list(dict.fromkeys(_gen_city_filters)) if _gen_city_filters else []
        gen_birth_year = _extract_birth_year_from_message(message)
        gen_lifecycle = (
            _birth_year_to_lifecycle(gen_birth_year)
            if gen_birth_year
            else _extract_lifecycle_from_message(message)
        )
        # 현재 메시지에서 생애주기 미감지 시 대화 히스토리에서 추출
        if not gen_lifecycle and messages:
            from app.chat.lifecycle import extract_lifecycle_from_history
            gen_lifecycle = extract_lifecycle_from_history(messages)
            if gen_lifecycle:
                logger.info(f"[RAG/general_v2] 히스토리에서 생애주기 추출: '{gen_lifecycle}'")
        logger.info(f"[RAG/general_v2] OKMS 필터 - sigun: {gen_sigun_filters}, lifecycle: '{gen_lifecycle}'")

        # OKMS 연도 필터 추출
        gen_year_filters = extract_year_filters(message)
        if gen_year_filters:
            logger.info(f"[RAG/general_v2] OKMS 연도 필터: {gen_year_filters}")
        else:
            logger.info("[RAG/general_v2] OKMS 연도 필터 미적용 (기본값: 올해)")

        # ====================================================================
        # OKMS 병렬 검색 헬퍼
        # ====================================================================
        def _group_a_run_okms_query(vector: str, keywords: str):
            """Group A 검색: vector/keyword를 균등 가중치로 Mariner에 전송, (keyword_docs, vector_docs) 반환"""
            try:
                return query_group_a_documents(
                    vector, keywords, Config.RAG_OKMS_COLLECTION,
                    year_filters=gen_year_filters or None,
                    sigun_filters=gen_sigun_filters,
                    lifecycle_filter=gen_lifecycle or None,
                    excluded_chunk_ids=excluded_chunk_ids,
                )
            except Exception as e:
                logger.warning(f"[RAG/general_v2] Group A 쿼리 검색 실패: {e}")
                return [], []

        def _group_a_run_okms_fallback(vector: str, keywords: str):
            """Group A Fallback: 연도/생애주기 제거, 시군 유지"""
            try:
                return query_group_a_fallback(
                    vector, keywords, Config.RAG_OKMS_COLLECTION,
                    sigun_filters=gen_sigun_filters,
                    excluded_chunk_ids=excluded_chunk_ids,
                )
            except Exception as e:
                logger.warning(f"[RAG/general_v2] Group A Fallback 검색 실패: {e}")
                return [], []

        def _run_gov_okms_query(search_str: str):
            """GOV_OKMS_V1 단일 검색: SIGUN/YEAR 필터 없음, LIFE_CYCLE만 선택 적용"""
            try:
                return query_gov_okms_documents(
                    search_str,
                    collection=Config.RAG_GOV_OKMS_COLLECTION,
                    lifecycle_filter=gen_lifecycle or None,
                    sigun_filters=gen_sigun_filters,
                    excluded_chunk_ids=excluded_chunk_ids,
                )
            except Exception as e:
                logger.warning(f"[RAG/general_v2] GOV_OKMS 쿼리 실패: {e}")
                return []

        loop = asyncio.get_event_loop()

        for i, sq in enumerate(ga_tri_built, 1):
            if sq:
                logger.info(f"[RAG/general_v2] [키워드검색어] #{i}: {sq}")

        # ====================================================================
        # Step 5-A: OKMS Group A + GOV_OKMS 단일 검색 (동시 병렬)
        # ====================================================================
        _GEN_GA_PER_QUERY = 5
        _GEN_GA_TOP_N = 10
        if status_callback:
            await status_callback("질문을 분석하고 있습니다")

        # eq(확장쿼리)와 sq(트리플쿼리)를 쌍으로 Group A 검색 (길이 맞춤, 빈 sq는 빈 문자열)
        from itertools import zip_longest
        ga_pair_futures = [
            loop.run_in_executor(None, _group_a_run_okms_query, eq, sq if sq else "")
            for eq, sq in zip_longest(expanded_queries, ga_tri_built, fillvalue="")
        ]
        gov_okms_futures = [
            loop.run_in_executor(None, _run_gov_okms_query, s)
            for s in list(expanded_queries) + [sq for sq in ga_tri_built if sq]
        ]
        if status_callback:
            await status_callback("문서를 검색하고 있습니다")
        _t = time.monotonic()
        all_results = await asyncio.gather(*ga_pair_futures, *gov_okms_futures)
        logger.info("[TIMING][general] Step5-A OKMS GroupA+GOV_OKMS 병렬 검색: %.3fs", time.monotonic() - _t)
        ga_pair_results = all_results[:len(ga_pair_futures)]
        gov_okms_results = all_results[len(ga_pair_futures):]

        # (keyword_docs, vector_docs) 튜플을 분리
        ga_vector_results = [pair[1] for pair in ga_pair_results]   # vector_docs → 확장쿼리 결과
        ga_keyword_results = [pair[0] for pair in ga_pair_results]  # keyword_docs → 트리플쿼리 결과

        okms_group_a_docs: List[Dict[str, Any]] = []
        for i, docs in enumerate(ga_vector_results, 1):
            if docs:
                okms_group_a_docs.extend(docs[:_GEN_GA_PER_QUERY])
                logger.info(f"[RAG/general_v2] [GroupA] OKMS 확장쿼리 #{i}: {min(len(docs), _GEN_GA_PER_QUERY)}개 문서")
            else:
                logger.info(f"[RAG/general_v2] [GroupA] OKMS 확장쿼리 #{i}: 0개 문서")
        for i, docs in enumerate(ga_keyword_results, 1):
            if not ga_tri_built[i - 1]:
                logger.info(f"[RAG/general_v2] [GroupA] OKMS 트리플쿼리 #{i}: 키워드 없음 - 건너뜀")
                continue
            if docs:
                okms_group_a_docs.extend(docs[:_GEN_GA_PER_QUERY])
                logger.info(f"[RAG/general_v2] [GroupA] OKMS 트리플쿼리 #{i}: {min(len(docs), _GEN_GA_PER_QUERY)}개 문서")
            else:
                logger.info(f"[RAG/general_v2] [GroupA] OKMS 트리플쿼리 #{i}: 0개 문서")

        gov_okms_docs: List[Dict[str, Any]] = []
        for i, docs in enumerate(gov_okms_results, 1):
            if docs:
                gov_okms_docs.extend(docs[:_GEN_GA_PER_QUERY])
                logger.info(f"[RAG/general_v2] [GOV_OKMS] #{i}: {min(len(docs), _GEN_GA_PER_QUERY)}개 문서")
            else:
                logger.info(f"[RAG/general_v2] [GOV_OKMS] #{i}: 0개 문서")

        okms_group_a_top = sorted(
            _deduplicate_documents(okms_group_a_docs + gov_okms_docs),
            key=lambda x: float(x.get("WEIGHT", 0) or 0),
            reverse=True,
        )[:_GEN_GA_TOP_N]
        logger.info(
            f"[RAG/general_v2] [GroupA+GOV_OKMS] 최종: {len(okms_group_a_top)}개 "
            f"(OKMS {len(okms_group_a_docs)}개 + GOV_OKMS {len(gov_okms_docs)}개 수집)"
        )
        for idx, doc in enumerate(okms_group_a_top, 1):
            logger.info(
                f"[RAG/general_v2] [GroupA] #{idx}"
                f"  NAME={doc.get('NAME', '')}"
                f"  WEIGHT={doc.get('WEIGHT', '')}"
                f"  YEAR={doc.get('YEAR', '')}"
                f"  SIGUN={doc.get('SIGUN', '')}"
                f"  CHUNK_ID={doc.get('CHUNK_ID', '')}"
            )

        # ====================================================================
        # Step 6: OKMS GroupA → top 5
        # ====================================================================
        _GEN_FINAL_TOP_N = 8
        _FALLBACK_THRESHOLD = 1
        okms_final = okms_group_a_top[:_GEN_FINAL_TOP_N]
        logger.info(f"[RAG/general_v2] OKMS 최종: {len(okms_final)}개 (GroupA {len(okms_group_a_top)}개)")

        # ====================================================================
        # Step 6-F: OKMS Fallback (필터 제거 재검색, 3건 미만 시)
        # ====================================================================
        if len(okms_final) < _FALLBACK_THRESHOLD:
            logger.info(f"[RAG/general_v2] OKMS 결과 {len(okms_final)}건 < {_FALLBACK_THRESHOLD}건 → Fallback 검색 시작")

            # Group A Fallback
            ga_fb_futures = [
                loop.run_in_executor(None, _group_a_run_okms_fallback, eq, sq if sq else "")
                for eq, sq in zip_longest(expanded_queries, ga_tri_built, fillvalue="")
            ]
            _t = time.monotonic()
            ga_fb_results = await asyncio.gather(*ga_fb_futures)
            logger.info("[TIMING][general] Step6-F OKMS Fallback 검색(병렬): %.3fs", time.monotonic() - _t)

            ga_fb_vector = [pair[1] for pair in ga_fb_results]
            ga_fb_keyword = [pair[0] for pair in ga_fb_results]

            okms_fb_a_docs: List[Dict[str, Any]] = []
            for i, docs in enumerate(ga_fb_vector, 1):
                if docs:
                    okms_fb_a_docs.extend(docs[:_GEN_GA_PER_QUERY])
            for i, docs in enumerate(ga_fb_keyword, 1):
                if ga_tri_built[i - 1] and docs:
                    okms_fb_a_docs.extend(docs[:_GEN_GA_PER_QUERY])

            # 기존 결과 + Fallback 결과 합산 → 중복 제거 → top 5
            okms_final = sorted(
                _deduplicate_documents(okms_final + okms_fb_a_docs),
                key=lambda x: float(x.get("WEIGHT", 0) or 0),
                reverse=True,
            )[:_GEN_FINAL_TOP_N]
            logger.info(f"[RAG/general_v2] OKMS Fallback 후: {len(okms_final)}개 (FB-A {len(okms_fb_a_docs)}개 추가)")

        # ====================================================================
        # Step 6-S: OKMS → WELFARE_TEL 전환 적합성 판단 (LLM)
        # ====================================================================
        _t = time.monotonic()
        okms_sufficiency = await retrieval_sufficiency_judgment(
            user_question=message,
            intent=intent,
            collection_name=Config.RAG_OKMS_COLLECTION,
            docs=okms_final,
        )
        logger.info("[TIMING][general] Step6-S OKMS 적합성 판단 [8b/sllm]: %.3fs", time.monotonic() - _t)
        logger.info(
            f"[RAG/general_v2] OKMS 적합성: sufficient={okms_sufficiency['sufficient']}, "
            f"reason={okms_sufficiency['reason']}"
        )

        # ====================================================================
        # Step 7: OUR_REGION_TEL 검색 (사용자 질문 키워드 기반, OKMS 부족 시)
        # ====================================================================
        _WELFARE_TEL_PER_QUERY = 5
        welfare_tel_docs: List[Dict[str, Any]] = []
        gsnd_top: List[Dict[str, Any]] = []

        fallback_expanded_queries: List[str] = []
        if okms_sufficiency["sufficient"]:
            logger.info("[RAG/general_v2] OKMS 결과 충분 — OUR_REGION_TEL/GSND 검색 생략")
        else:
            # 2차 fallback: 확장 쿼리 사용
            if status_callback:
                await status_callback("검색을 보강하고 있습니다")
            _t = time.monotonic()
            if precomputed_expanded_queries:
                _candidates = precomputed_expanded_queries
                logger.info("[RAG/general_v2] fallback: 사전 계산 확장 쿼리 사용")
            else:
                _candidates = await expand_query(reformed_query)
            logger.info("[TIMING][general] Step6-S2 fallback 쿼리확장: %.3fs", time.monotonic() - _t)
            fallback_expanded_queries = [
                q for q in (str(x).strip() for x in (_candidates or []))
                if q and q != reformed_query
            ]
            if fallback_expanded_queries:
                logger.info("[RAG/general_v2] fallback 확장 쿼리 적용: %d개", len(fallback_expanded_queries))
            else:
                logger.info("[RAG/general_v2] fallback 확장 쿼리 없음 — 정제 질의 유지")
            logger.info("[RAG/general_v2] OKMS 결과 부족 → OUR_REGION_TEL 검색 진행")

            from app.chat.sigun import extract_eupmyeondong_from_message
            gen_eupmyeondong = extract_eupmyeondong_from_message(message)
            gen_eupmyeondong_filters = [gen_eupmyeondong] if gen_eupmyeondong else []

            _t = time.monotonic()
            welfare_tel_docs = await run_welfare_tel_queries(
                message, gen_sigun_filters, gen_eupmyeondong_filters,
                _WELFARE_TEL_PER_QUERY, "RAG/general_v2",
                excluded_chunk_ids=excluded_chunk_ids,
            )
            logger.info("[TIMING][general] Step7 OUR_REGION_TEL 검색: %.3fs", time.monotonic() - _t)
            logger.info(f"[RAG/general_v2] OUR_REGION_TEL 검색 합계: {len(welfare_tel_docs)}개")

            # ================================================================
            # Step 7-S: WELFARE_TEL → GSND 전환 적합성 판단 (LLM)
            # ================================================================
            welfare_combined = sorted(
                _deduplicate_documents(okms_final + welfare_tel_docs),
                key=lambda x: float(x.get("WEIGHT", 0) or 0),
                reverse=True,
            )
            _t = time.monotonic()
            welfare_sufficiency = await retrieval_sufficiency_judgment(
                user_question=message,
                intent=intent,
                collection_name=Config.RAG_WELFARE_TEL_COLLECTION,
                docs=welfare_combined,
            )
            logger.info("[TIMING][general] Step7-S OUR_REGION_TEL 적합성 판단 [8b/sllm]: %.3fs", time.monotonic() - _t)
            logger.info(
                f"[RAG/general_v2] OKMS+OUR_REGION_TEL 적합성: sufficient={welfare_sufficiency['sufficient']}, "
                f"reason={welfare_sufficiency['reason']}"
            )

            # ================================================================
            # Step 7-B: GSND 보강 검색 (OKMS+OUR_REGION_TEL 부족 시)
            # ================================================================
            if welfare_sufficiency["sufficient"]:
                logger.info("[RAG/general_v2] OKMS+OUR_REGION_TEL 결과 충분 — GSND 검색 생략")
            else:
                logger.info("[RAG/general_v2] OKMS+OUR_REGION_TEL 결과 부족 → GSND 병렬 검색")
                _GSND_SUPPLEMENT_COUNT = 2
                _GSND_PER_QUERY = 10
                gsnd_seed_queries = fallback_expanded_queries or list(expanded_queries)
                gsnd_all_queries = list(gsnd_seed_queries) + list(search_queries)

                gsnd_year_filters = extract_year_filters(message)
                if gsnd_year_filters:
                    logger.info(f"[RAG/general_v2] GSND 연도 필터: {gsnd_year_filters}")
                else:
                    logger.info("[RAG/general_v2] GSND 연도 필터 미적용 (기본값: 올해)")

                def _run_gsnd_query(query):
                    try:
                        docs = query_GSND_general_documents(
                            query, selected_collection,
                            sigun_filters=gen_sigun_filters,
                            year_filters=gsnd_year_filters or None,
                            excluded_chunk_ids=excluded_chunk_ids,
                        )
                        if docs:
                            return sorted(
                                docs,
                                key=lambda x: float(x.get("WEIGHT", 0) or 0),
                                reverse=True,
                            )[:_GSND_PER_QUERY]
                    except Exception as e:
                        logger.warning(f"[RAG/general_v2] GSND 쿼리 검색 실패: {e}")
                    return []

                gsnd_futures = [
                    loop.run_in_executor(None, _run_gsnd_query, q)
                    for q in gsnd_all_queries
                ]
                _t = time.monotonic()
                gsnd_results = await asyncio.gather(*gsnd_futures)
                logger.info("[TIMING][general] Step7-B GSND 보강 검색(병렬): %.3fs", time.monotonic() - _t)

                gsnd_docs: List[Dict[str, Any]] = []
                for i, (q, result) in enumerate(zip(gsnd_all_queries, gsnd_results), 1):
                    if result:
                        gsnd_docs.extend(result)
                        logger.info(f"[RAG/general_v2] GSND 병렬쿼리 #{i}: {len(result)}개 문서")

                gsnd_top = sorted(
                    _deduplicate_documents(gsnd_docs),
                    key=lambda x: float(x.get("WEIGHT", 0) or 0),
                    reverse=True,
                )[:_GSND_SUPPLEMENT_COUNT]
                logger.info(f"[RAG/general_v2] GSND 검색 합계: {len(gsnd_docs)}개 → 상위 {len(gsnd_top)}개 보강")

        # ====================================================================
        # Step 7 합산: OKMS + WELFARE_TEL + GSND
        # ====================================================================
        top_docs = sorted(
            _deduplicate_documents(okms_final + welfare_tel_docs + gsnd_top),
            key=lambda x: float(x.get("WEIGHT", 0) or 0),
            reverse=True,
        )

        logger.info(f"[RAG/general_v2] 최종 선택: {len(top_docs)}개 문서")
        for i, doc in enumerate(top_docs, 1):
            logger.info(f"[RAG/general_v2] #{i} NAME={_get_document_name(doc) or '?'}, WEIGHT={doc.get('WEIGHT', '?')}")

        # ====================================================================
        # Step 7-C: LLM 관련성 필터 (무관 문서 제거) — 비활성화
        # ====================================================================
        if status_callback:
            await status_callback("검색 결과를 검증하고 있습니다")
        _t = time.monotonic()
        top_docs = await filter_irrelevant_docs(reformed_query, top_docs, sigun_filters=gen_sigun_filters)
        logger.info("[TIMING][general] Step7-C 관련성 필터 [8b/sllm]: %.3fs", time.monotonic() - _t)
        logger.info(f"[RAG/general_v2] 관련성 필터 후: {len(top_docs)}개 문서")

        # ====================================================================
        # Step 7-C-3: 관련성 필터 0건 → GroupA 하위 문서 재시도
        # (상위 N건이 모두 무관 판정된 경우 top-N 이후 문서를 추가 시도)
        # ====================================================================
        if not top_docs:
            lower_docs = okms_group_a_top[_GEN_FINAL_TOP_N:]
            if lower_docs:
                logger.info(
                    f"[RAG/general_v2] 관련성 필터 0건 → GroupA 하위 {len(lower_docs)}건 재시도"
                )
                _t = time.monotonic()
                top_docs = await filter_irrelevant_docs(
                    reformed_query, lower_docs, sigun_filters=gen_sigun_filters
                )
                logger.info(
                    "[TIMING][general] Step7-C-3 GroupA 하위 재시도: %.3fs", time.monotonic() - _t
                )
                logger.info(f"[RAG/general_v2] GroupA 하위 재시도 결과: {len(top_docs)}건")

        # ====================================================================
        # Step 7-C-4: 재검색
        # - 1차 결과가 0건인 경우
        # - 또는 추가 요청 등으로 기존 노출 문서를 제외해야 하는 경우
        # ====================================================================
        if (not top_docs) or excluded_chunk_ids or excluded_service_names:
            logger.info("[RAG/general_v2] 재검색 시작 (0건 또는 제외문서 기반 추가 탐색)")
            _t = time.monotonic()
            fallback_seed_queries = fallback_expanded_queries or list(expanded_queries)
            fb_futures = [
                loop.run_in_executor(None, _group_a_run_okms_fallback, eq, sq if sq else "")
                for eq, sq in zip_longest(fallback_seed_queries, ga_tri_built, fillvalue="")
            ]
            fb_results = await asyncio.gather(*fb_futures)
            logger.info(
                "[TIMING][general] Step7-C-4 재검색: %.3fs", time.monotonic() - _t
            )

            fb_docs: List[Dict[str, Any]] = []
            for pair in fb_results:
                for part in (pair[0], pair[1]):
                    if part:
                        fb_docs.extend(part[:_GEN_GA_PER_QUERY])

            if fb_docs:
                fb_pool = sorted(
                    _deduplicate_documents(fb_docs),
                    key=lambda x: float(x.get("WEIGHT", 0) or 0),
                    reverse=True,
                )

                # 추가 요청 시에는 후보 풀 단계에서 먼저 제외
                if excluded_chunk_ids or excluded_service_names:
                    fb_pool = filter_excluded_docs(
                        fb_pool,
                        excluded_chunk_ids or [],
                        excluded_service_names or [],
                    )
                    logger.info(f"[MoreResults][general] 재검색 후보 제외 후: {len(fb_pool)}개 문서")

                fb_pool = fb_pool[:_GEN_GA_TOP_N]

                _t = time.monotonic()
                top_docs = await filter_irrelevant_docs(
                    reformed_query, fb_pool, sigun_filters=gen_sigun_filters
                )
                logger.info(
                    "[TIMING][general] Step7-C-4 재검색 관련성 필터: %.3fs", time.monotonic() - _t
                )
                logger.info(f"[RAG/general_v2] 재검색 결과: {len(top_docs)}건")

        # 최종 안전망
        if excluded_chunk_ids or excluded_service_names:
            top_docs = filter_excluded_docs(top_docs, excluded_chunk_ids or [], excluded_service_names)
            logger.info(f"[MoreResults][general] 최종 제외 필터 후: {len(top_docs)}개 문서")


        # # ====================================================================
        # # Step 7-C-4: 여전히 0건 → Fallback 재검색 (연도/생애주기 필터 제거)
        # # ====================================================================
        # if not top_docs:
        #     logger.info("[RAG/general_v2] 관련성 필터 0건 → Fallback 재검색 시작")
        #     _t = time.monotonic()
        #     fb_futures = [
        #         loop.run_in_executor(None, _group_a_run_okms_fallback, eq, sq if sq else "")
        #         for eq, sq in zip_longest(expanded_queries, ga_tri_built, fillvalue="")
        #     ]
        #     fb_results = await asyncio.gather(*fb_futures)
        #     logger.info(
        #         "[TIMING][general] Step7-C-4 Fallback 재검색: %.3fs", time.monotonic() - _t
        #     )
        #     fb_docs: List[Dict[str, Any]] = []
        #     for pair in fb_results:
        #         for part in (pair[0], pair[1]):
        #             if part:
        #                 fb_docs.extend(part[:_GEN_GA_PER_QUERY])
        #     if fb_docs:
        #         fb_pool = sorted(
        #             _deduplicate_documents(fb_docs),
        #             key=lambda x: float(x.get("WEIGHT", 0) or 0),
        #             reverse=True,
        #         )[:_GEN_GA_TOP_N]
        #         _t = time.monotonic()
        #         top_docs = await filter_irrelevant_docs(
        #             reformed_query, fb_pool, sigun_filters=gen_sigun_filters
        #         )
        #         logger.info(
        #             "[TIMING][general] Step7-C-4 Fallback 관련성 필터: %.3fs", time.monotonic() - _t
        #         )
        #         logger.info(f"[RAG/general_v2] Fallback 재검색 결과: {len(top_docs)}건")
        #
        # if excluded_chunk_ids or excluded_service_names:
        #     top_docs = filter_excluded_docs(top_docs, excluded_chunk_ids or [], excluded_service_names)
        #     logger.info(f"[MoreResults][general] 제외 필터 후: {len(top_docs)}개 문서")

        # ====================================================================
        # Step 7-C-2: 0건 → 바로 "답변을 찾을 수 없습니다" 반환
        # ====================================================================
        if not top_docs:
            logger.info("[RAG/general_v2] 최종 문서 0건 → '답변을 찾을 수 없습니다' 반환")
            return "죄송합니다. 요청하신 내용에 대한 답변을 찾을 수 없습니다.", []

        # ====================================================================
        # Step 7-D: 웨이트 상위 30% → 최신순 / 나머지 → 웨이트 내림차순
        # ====================================================================
        top_docs = sort_weight_top30_then_year(top_docs)

        # ====================================================================
        # Step 8: 참고 문서 구성
        # ====================================================================
        referenced_documents = build_referenced_documents(top_docs)
        logger.info(f"[RAG/general_v2] UI 표시용 문서: {len(referenced_documents)}개")

        # ====================================================================
        # Step 9: 최종 응답 생성
        # ====================================================================
        _t = time.monotonic()
        _final_user_msg = (final_user_message or "").strip() or message
        response = await generate_final_response_v2(
            _final_user_msg, top_docs, temperature, max_tokens, stream,
            frequency_penalty, repetition_penalty, top_p, top_k, seed, tools,
            intent=intent,
            lifecycle=gen_lifecycle,
            messages=messages,
        )
        logger.info("[TIMING][general] Step9 최종 응답 생성 [32b/luxia]: %.3fs", time.monotonic() - _t)
        logger.info("[TIMING][general] process_rag_general 전체: %.3fs", time.monotonic() - t_total)
        return response, referenced_documents

    except Exception as e:
        logger.error(f"[RAG/general_v2] 처리 중 오류: {e}", exc_info=True)
        raise
