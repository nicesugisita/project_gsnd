"""
RAG 서비스 — general 전용 플로우

general 의도의 검색 및 응답 생성을 별도 파일로 분리한 것입니다.
로직 자체는 기존 services/rag_service.py의 general 플로우와 동일합니다.

기존 services/rag_service.py는 변경하지 않습니다.
"""

import logging
import asyncio
import time
from typing import Dict, Any, List, Optional, Tuple

from app.core.config import Config

# general 전용 Mariner 쿼리셋
from app.mariner.queryset_okms import query_group_a_documents
from app.mariner.queryset_gov_okms import query_gov_okms_documents
from app.mariner.queryset_gsnd import query_GSND_general_documents
from .common import (
    sort_weight_top30_then_year,
    build_referenced_documents,
    filter_excluded_docs,
    dedupe_cap_expanded_queries,
    apply_policy_priority_to_documents,
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
    filter_okms_keywords,
)
from .response_generator import generate_final_response_v2
from app.chat.routing import (
    expand_query,
    extract_triples,
)
from app.mariner.sigun_utils import normalize_sigun
from app.shared.utils.year_filter import extract_year_filters
from app.shared.utils.relevance_filter import filter_irrelevant_docs
from .pipeline_utils import (
    collect_okms_groupa_and_gov_docs,
    collect_okms_groupa_fallback_docs,
)

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
    precomputed_search_target: Optional[str] = None,
    precomputed_policy_priority_tag: Optional[str] = None,
    more_detail: bool = False,
) -> tuple[Any, List[Dict[str, str]]]:
    """
    RAG 문서 검색 및 최종 응답 생성 — general 전용

    general 의도일 때만 이 함수의 로직을 실행하며,
    그 외 의도는 기존 함수로 위임합니다.
    """

    _ = precomputed_search_target

    try:
        t_total = time.monotonic()
        _skip_policy_boost = bool(excluded_chunk_ids or excluded_service_names)

        # Step 1: 쿼리 확장 (Mariner 검색 전)
        if precomputed_expanded_queries:
            expanded_queries = dedupe_cap_expanded_queries(
                precomputed_expanded_queries, reformed_query=reformed_query
            )
            logger.info(
                "[RAG/general_v2] 사전 계산된 확장 쿼리 사용: 원본 %d개 → 캡 후 %d개",
                len(precomputed_expanded_queries or []),
                len(expanded_queries),
            )
        else:
            if status_callback:
                await status_callback("최적의 답변방식을 찾고 있습니다")
            _t = time.monotonic()
            expanded_queries = await expand_query(reformed_query)
            logger.info("[TIMING][general] Step2 쿼리 확장: %.3fs", time.monotonic() - _t)
            if not expanded_queries:
                logger.warning("[RAG/general_v2] 쿼리 확장 실패 - 원본 질의 사용")
                expanded_queries = [reformed_query]
        if not expanded_queries:
            expanded_queries = [reformed_query]
        logger.info(f"[RAG/general_v2] 확장 완료: {len(expanded_queries)}개 쿼리")
        for i, eq in enumerate(expanded_queries, 1):
            logger.debug(f"[RAG/general_v2] [벡터검색어] #{i}: {eq}")

        # Step 3: 트리플(키워드) 추출 (확장쿼리별)
        if status_callback:
            await status_callback("내용을 정리하고 있습니다")
        _t = time.monotonic()
        if precomputed_keywords:
            triples_list = [filter_okms_keywords(precomputed_keywords)]
            if len(expanded_queries) > 1:
                triples_list.extend([[] for _ in range(len(expanded_queries) - 1)])
            logger.info("[RAG/general_v2] 사전 계산된 키워드 사용: %d개", len(precomputed_keywords or []))
            logger.debug("[RAG/general_v2] 사전 계산된 키워드 상세: %s", precomputed_keywords)
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
            logger.debug(f"[RAG/general_v2] 외부 sigun_filters 사용: {gen_sigun_filters}")
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
                logger.debug(f"[RAG/general_v2] 히스토리에서 생애주기 추출: '{gen_lifecycle}'")
        logger.debug(f"[RAG/general_v2] OKMS 필터 - sigun: {gen_sigun_filters}, lifecycle: '{gen_lifecycle}'")

        # OKMS 연도 필터 추출
        gen_year_filters = extract_year_filters(message)
        if gen_year_filters:
            logger.debug(f"[RAG/general_v2] OKMS 연도 필터: {gen_year_filters}")
        else:
            logger.debug("[RAG/general_v2] OKMS 연도 필터 미적용 (기본값: 올해)")

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
            """Group A Fallback: 정상 검색 함수 재사용, 생애주기/사업명 앵커 비활성(시군/연도/제외 유지)"""
            try:
                return query_group_a_documents(
                    vector, keywords, Config.RAG_OKMS_COLLECTION,
                    year_filters=gen_year_filters or None,
                    sigun_filters=gen_sigun_filters,
                    lifecycle_filter=None,
                    excluded_chunk_ids=excluded_chunk_ids,
                    apply_business_anchor=False,
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
                logger.debug(f"[RAG/general_v2] [키워드검색어] #{i}: {sq}")

        # ====================================================================
        # Step 5-A: OKMS Group A + GOV_OKMS + GSND_DATASET_V8 (동시 병렬)
        #   - 기존엔 OKMS 적합성 LLM 판단 후 부족할 때만 GSND 보강
        #   - 변경: general 의도에서는 GSND_DATASET_V8 을 항상 검색하고
        #     OKMS 수집과 병렬로 실행해 wall-clock 손실을 최소화한다.
        # ====================================================================
        _GEN_GA_PER_QUERY = 5
        _GEN_GA_TOP_N = 20 if more_detail else 10
        _GSND_PER_QUERY = 10
        _GSND_STAGE_MAX_SEC = 15.0
        _GSND_TIMEOUT_BREAK_THRESHOLD = 2
        if more_detail:
            logger.info(
                "[RAG/general_v2] MORE_DETAIL 후속 — 동일 쿼리로 더 많은 문서 회수 모드 (GA_TOP_N=%d)",
                _GEN_GA_TOP_N,
            )

        # GSND 시드 쿼리: 1차 expanded_queries + 키워드 검색쿼리
        gsnd_seed_queries = list(expanded_queries)
        gsnd_all_queries = list(gsnd_seed_queries) + list(search_queries)
        gsnd_year_filters = gen_year_filters
        if gsnd_year_filters:
            logger.info(f"[RAG/general_v2] GSND 연도 필터: {gsnd_year_filters}")
        else:
            logger.info("[RAG/general_v2] GSND 연도 필터 미적용 (기본값: 올해)")

        def _run_gsnd_query(query: str) -> Tuple[List[Dict[str, Any]], bool]:
            for attempt in range(2):
                try:
                    use_year_filter = (attempt == 0)
                    gsnd_search_mode = "hybrid" if attempt == 0 else "keyword_only"
                    # GSND_DATASET_V8 은 광역 정책 문서(기초연금 등)의 SIGUN 메타가
                    # 균일하지 않아 시군 필터를 적용하면 Mariner 서버 단계에서 통째로
                    # 제외되는 문제가 있다. 진단/완화를 위해 시군 필터 미적용.
                    docs = query_GSND_general_documents(
                        query, Config.RAG_COLLECTION,
                        sigun_filters=None,
                        year_filters=gsnd_year_filters or None,
                        excluded_chunk_ids=excluded_chunk_ids,
                        apply_year_filter=use_year_filter,
                        search_mode=gsnd_search_mode,
                    )
                    if docs:
                        return sorted(
                            docs,
                            key=lambda x: float(x.get("WEIGHT", 0) or 0),
                            reverse=True,
                        )[:_GSND_PER_QUERY], False
                    return [], False
                except Exception as e:
                    if attempt == 0 and "-60004" in str(e):
                        logger.warning(
                            "[RAG/general_v2] GSND 쿼리 타임아웃(-60004) 재시도(연도필터 해제): %s",
                            query[:80],
                        )
                        continue
                    if "-60004" in str(e):
                        logger.warning(
                            "[RAG/general_v2] GSND 쿼리 타임아웃 누적 후보: %s",
                            query[:80],
                        )
                        return [], True
                    logger.warning(f"[RAG/general_v2] GSND 쿼리 검색 실패: {e}")
                    return [], False
            return [], False

        async def _run_gsnd_supplement_search() -> List[Dict[str, Any]]:
            """GSND_DATASET_V8 보강 검색 — 내부는 순차(동시성 폭증 방지),
            외부에선 OKMS 수집과 병렬 실행."""
            t_start = time.monotonic()
            results: List[List[Dict[str, Any]]] = []
            timeout_count = 0
            for q in gsnd_all_queries:
                elapsed = time.monotonic() - t_start
                if elapsed > _GSND_STAGE_MAX_SEC:
                    logger.warning(
                        "[RAG/general_v2] GSND 시간 상한(%.0fs) 초과로 중단",
                        _GSND_STAGE_MAX_SEC,
                    )
                    break
                remaining = max(_GSND_STAGE_MAX_SEC - elapsed, 0.0)
                try:
                    docs, timed_out = await asyncio.wait_for(
                        loop.run_in_executor(None, _run_gsnd_query, q),
                        timeout=remaining,
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[RAG/general_v2] GSND 단일 쿼리 시간 초과로 다음 단계 진행",
                    )
                    break
                results.append(docs)
                if timed_out:
                    timeout_count += 1
                    if timeout_count >= _GSND_TIMEOUT_BREAK_THRESHOLD:
                        logger.warning(
                            "[RAG/general_v2] GSND 타임아웃 누적 %d회로 보강 검색 조기 중단",
                            timeout_count,
                        )
                        break
            flat: List[Dict[str, Any]] = []
            for i, (q, result) in enumerate(zip(gsnd_all_queries, results), 1):
                if result:
                    flat.extend(result)
                logger.debug(f"[RAG/general_v2] GSND 쿼리 #{i}: {len(result)}개 문서")
            return flat

        if status_callback:
            await status_callback("질문을 분석하고 있습니다")

        _t = time.monotonic()
        okms_task = asyncio.create_task(
            collect_okms_groupa_and_gov_docs(
                message=message,
                reformed_query=reformed_query,
                policy_priority_tag=precomputed_policy_priority_tag,
                expanded_queries=expanded_queries,
                tri_built=ga_tri_built,
                per_query_limit=_GEN_GA_PER_QUERY,
                run_group_a=_group_a_run_okms_query,
                run_gov=_run_gov_okms_query,
                log_prefix="RAG/general_v2",
                status_callback=status_callback,
                log_skip_empty_triple=True,
                policy_search_boost_enabled=not _skip_policy_boost,
            )
        )
        gsnd_task = asyncio.create_task(_run_gsnd_supplement_search())
        (okms_group_a_docs, gov_okms_docs), gsnd_docs = await asyncio.gather(
            okms_task, gsnd_task
        )
        logger.info(
            "[TIMING][general] Step5-A OKMS+GOV_OKMS+GSND 병렬 검색: %.3fs",
            time.monotonic() - _t,
        )

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
            logger.debug(
                f"[RAG/general_v2] [GroupA] #{idx}"
                f"  NAME={doc.get('NAME', '')}"
                f"  WEIGHT={doc.get('WEIGHT', '')}"
                f"  YEAR={doc.get('YEAR', '')}"
                f"  SIGUN={doc.get('SIGUN', '')}"
                f"  CHUNK_ID={doc.get('CHUNK_ID', '')}"
            )

        # ====================================================================
        # Step 6: OKMS GroupA → top N
        # ====================================================================
        _GEN_FINAL_TOP_N = 15 if more_detail else 8
        _FALLBACK_THRESHOLD = 1
        okms_final = okms_group_a_top[:_GEN_FINAL_TOP_N]
        logger.info(f"[RAG/general_v2] OKMS 최종: {len(okms_final)}개 (GroupA {len(okms_group_a_top)}개)")

        # ====================================================================
        # Step 6-F: OKMS Fallback (필터 제거 재검색, 3건 미만 시)
        # ====================================================================
        if len(okms_final) < _FALLBACK_THRESHOLD:
            logger.info(f"[RAG/general_v2] OKMS 결과 {len(okms_final)}건 < {_FALLBACK_THRESHOLD}건 → Fallback 검색 시작")

            _t = time.monotonic()
            okms_fb_a_docs = await collect_okms_groupa_fallback_docs(
                message=message,
                reformed_query=reformed_query,
                policy_priority_tag=precomputed_policy_priority_tag,
                expanded_queries=expanded_queries,
                tri_built=ga_tri_built,
                per_query_limit=_GEN_GA_PER_QUERY,
                run_group_a_fallback=_group_a_run_okms_fallback,
                policy_search_boost_enabled=not _skip_policy_boost,
            )
            logger.info("[TIMING][general] Step6-F OKMS Fallback 검색(병렬): %.3fs", time.monotonic() - _t)

            # 기존 결과 + Fallback 결과 합산 → 중복 제거 → top 5
            okms_final = sorted(
                _deduplicate_documents(okms_final + okms_fb_a_docs),
                key=lambda x: float(x.get("WEIGHT", 0) or 0),
                reverse=True,
            )[:_GEN_FINAL_TOP_N]
            logger.info(f"[RAG/general_v2] OKMS Fallback 후: {len(okms_final)}개 (FB-A {len(okms_fb_a_docs)}개 추가)")

        # ====================================================================
        # Step 7: GSND 결과 정리 (적합성 판단 없이 항상 합산)
        # ====================================================================
        _GSND_SUPPLEMENT_COUNT = 7 if more_detail else 3
        gsnd_top = sorted(
            _deduplicate_documents(gsnd_docs),
            key=lambda x: float(x.get("WEIGHT", 0) or 0),
            reverse=True,
        )[:_GSND_SUPPLEMENT_COUNT]
        logger.info(
            f"[RAG/general_v2] GSND 검색 합계: {len(gsnd_docs)}개 → 상위 {len(gsnd_top)}개 보강"
        )

        # ====================================================================
        # Step 7 합산: OKMS + GSND
        # ====================================================================
        top_docs = sorted(
            _deduplicate_documents(okms_final + gsnd_top),
            key=lambda x: float(x.get("WEIGHT", 0) or 0),
            reverse=True,
        )

        logger.info(f"[RAG/general_v2] 최종 선택: {len(top_docs)}개 문서")
        for i, doc in enumerate(top_docs, 1):
            logger.debug(f"[RAG/general_v2] #{i} NAME={_get_document_name(doc) or '?'}, WEIGHT={doc.get('WEIGHT', '?')}")

        # ====================================================================
        # Step 7-C: LLM 관련성 필터 (무관 문서 제거) — 비활성화
        # ====================================================================
        if status_callback:
            await status_callback("검색 결과를 검증하고 있습니다")
        _t = time.monotonic()
        top_docs = apply_policy_priority_to_documents(
            precomputed_policy_priority_tag,
            top_docs,
            log_prefix="[RAG/general_v2]",
            apply_enabled=not _skip_policy_boost,
        )
        top_docs = await filter_irrelevant_docs(
            reformed_query,
            top_docs,
            sigun_filters=gen_sigun_filters,
            max_judgment_docs=20 if more_detail else None,
        )
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
                lower_docs = apply_policy_priority_to_documents(
                    precomputed_policy_priority_tag,
                    lower_docs,
                    log_prefix="[RAG/general_v2][C3]",
                    apply_enabled=not _skip_policy_boost,
                )
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
            _raw_seed = list(expanded_queries)
            fallback_seed_queries = dedupe_cap_expanded_queries(
                _raw_seed, reformed_query=reformed_query
            ) or [reformed_query]
            fb_results = await collect_okms_groupa_fallback_docs(
                message=message,
                reformed_query=reformed_query,
                policy_priority_tag=precomputed_policy_priority_tag,
                expanded_queries=fallback_seed_queries,
                tri_built=ga_tri_built,
                per_query_limit=_GEN_GA_PER_QUERY,
                run_group_a_fallback=_group_a_run_okms_fallback,
                max_policy_pairs=1,
                policy_search_boost_enabled=not _skip_policy_boost,
            )
            logger.info(
                "[TIMING][general] Step7-C-4 재검색: %.3fs", time.monotonic() - _t
            )

            fb_docs: List[Dict[str, Any]] = list(fb_results)

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
                fb_pool = apply_policy_priority_to_documents(
                    precomputed_policy_priority_tag,
                    fb_pool,
                    log_prefix="[RAG/general_v2][C4]",
                    apply_enabled=not _skip_policy_boost,
                )

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
            more_info_mode=more_detail or bool(excluded_chunk_ids or excluded_service_names),
            detail_requested=bool(more_detail),
        )
        logger.info("[TIMING][general] Step9 최종 응답 생성 [32b/luxia]: %.3fs", time.monotonic() - _t)
        logger.info("[TIMING][general] process_rag_general 전체: %.3fs", time.monotonic() - t_total)
        return response, referenced_documents

    except Exception as e:
        logger.error(f"[RAG/general_v2] 처리 중 오류: {e}", exc_info=True)
        raise
