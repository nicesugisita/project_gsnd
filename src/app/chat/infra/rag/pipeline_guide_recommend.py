"""
RAG 서비스 — guide_recommend 전용 플로우

guide_recommend 의도의 검색 및 응답 생성을 별도 파일로 분리한 것입니다.
로직 자체는 기존 services/rag_service.py의 guide_recommend 플로우와 동일합니다.

기존 services/rag_service.py는 변경하지 않습니다.
"""

import logging
import asyncio
import time
from typing import Dict, Any, List, Optional

from app.core.config import Config

# OKMS Mariner 쿼리셋
from app.mariner.queryset_okms import query_group_a_documents
from app.mariner.queryset_gov_okms import query_gov_okms_documents
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
from app.chat.routing import expand_query
from app.shared.utils.keyword_extractor import extract_nouns
from app.mariner.sigun_utils import normalize_sigun
from app.shared.utils.year_filter import extract_year_filters
from app.shared.utils.relevance_filter import filter_irrelevant_docs
from .policy_priority import resolve_policy_boost_keywords
from .pipeline_utils import (
    collect_okms_groupa_and_gov_docs,
    collect_okms_groupa_fallback_docs,
    collect_okms_groupa_and_gov_fallback_docs,
)
logger = logging.getLogger(__name__)


async def process_rag_guide_recommend(
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
    intent: str = "guide_recommend",
    messages: list = None,
    sigun_filters: Optional[List[str]] = None,
    precomputed_expanded_queries: list = None,
    precomputed_keywords: list = None,
    excluded_chunk_ids: List[str] = None,
    excluded_service_names: List[str] = None,
    final_user_message: Optional[str] = None,
    recommended_question_prompt: bool = False,
    precomputed_search_target: Optional[str] = None,
    precomputed_policy_priority_tag: Optional[str] = None,
) -> tuple[Any, List[Dict[str, str]]]:
    """
    RAG 문서 검색 및 최종 응답 생성 — guide_recommend 전용

    guide_recommend 의도일 때만 이 함수의 로직을 실행하며,
    그 외 의도는 기존 함수로 위임합니다.
    """
    # guide_recommend가 아닌 경우 기존 함수로 위임
    _ = precomputed_search_target

    try:
        t_total = time.monotonic()
        _skip_policy_boost = bool(excluded_chunk_ids or excluded_service_names)
        policy_tags, _ = resolve_policy_boost_keywords(precomputed_policy_priority_tag)
        # guide_recommend에서 low_income 태그는 query-time 부스트를 끄고,
        # elderly/implant 등은 기존 우선 정책을 유지한다.
        _query_policy_boost_enabled = (not _skip_policy_boost) and ("low_income" not in policy_tags)
        selected_collection = Config.RAG_OKMS_COLLECTION
        logger.debug(f"[RAG/guide_recommend_v2] 컬렉션: {selected_collection}")

        # ====================================================================
        # guide_recommend + OKMS 컬렉션 전용 플로우
        # ====================================================================

        # Step 0: 시군/출생연도/생애주기 추출
        # sigun_filters가 외부에서 전달된 경우(히스토리/위치명 기반 확정값) 그대로 사용
        if sigun_filters is not None:
            gr_sigun_filters = sigun_filters
            # sigun_raws: 정규화된 값에서 시군명 부분만 추출 (LLM 표시용)
            sigun_raws = [s.replace("경상남도 ", "") for s in sigun_filters]
            logger.debug(f"[RAG/guide_recommend_v2] 외부 sigun_filters 사용: {gr_sigun_filters}")
        else:
            sigun_raws = _extract_sigun_from_message(message) or _extract_sigun_from_message(reformed_query)
            logger.debug(f"[RAG/guide_recommend_v2] 추출된 시군: {sigun_raws}")
            _gr_normalized = [normalize_sigun(r) for r in sigun_raws if r != "경남"]
            _gr_city_filters = [s for s in _gr_normalized if s.startswith("경상남도 ")]
            gr_sigun_filters = list(dict.fromkeys(_gr_city_filters)) if _gr_city_filters else []
        logger.debug(f"[RAG/guide_recommend_v2] sigun 필터: {gr_sigun_filters}")

        birth_year = _extract_birth_year_from_message(message)

        if birth_year:
            lifecycle = _birth_year_to_lifecycle(birth_year)
            logger.debug(f"[RAG/guide_recommend_v2] 출생연도: {birth_year} → 생애주기: '{lifecycle}'")
        else:
            lifecycle = _extract_lifecycle_from_message(message)
            if lifecycle:
                logger.debug(f"[RAG/guide_recommend_v2] 생애주기 키워드 직접 추출: '{lifecycle}'")
            else:
                logger.debug(f"[RAG/guide_recommend_v2] 출생연도 추출 불가, 생애주기 필터 미적용")

        # 연도 필터: 질의에 연도 없으면 현재 연도 기본 적용
        gr_year_filters = extract_year_filters(message)
        if gr_year_filters:
            logger.debug(f"[RAG/guide_recommend_v2] 연도 필터: {gr_year_filters}")
        else:
            logger.debug(f"[RAG/guide_recommend_v2] 연도 필터 미적용")

        # Step A-1: 쿼리 확장 (Mariner 검색 전)
        gr_expand_base = reformed_query
        if precomputed_expanded_queries:
            gr_expanded = dedupe_cap_expanded_queries(
                precomputed_expanded_queries, reformed_query=gr_expand_base
            )
            logger.info(
                "[RAG/guide_recommend_v2] 사전 계산된 확장 쿼리 사용: 원본 %d개 → 캡 후 %d개",
                len(precomputed_expanded_queries or []),
                len(gr_expanded),
            )
        else:
            if status_callback:
                await status_callback("최적의 답변방식을 찾고 있습니다")
            _t = time.monotonic()
            gr_expanded = await expand_query(gr_expand_base)
            logger.info("[TIMING][guide_recommend] StepA-1 쿼리 확장: %.3fs", time.monotonic() - _t)
            if not gr_expanded:
                logger.warning("[RAG/guide_recommend_v2] 쿼리 확장 실패 - 기준 질의 사용")
                gr_expanded = [gr_expand_base]
        if not gr_expanded:
            gr_expanded = [gr_expand_base]
        logger.info(f"[RAG/guide_recommend_v2] 확장 완료: {len(gr_expanded)}개 쿼리")
        for i, eq in enumerate(gr_expanded, 1):
            logger.debug(f"[RAG/guide_recommend_v2] [벡터검색어] #{i}: {eq}")

        # Step A-2: 벡터 검색어에서 키워드 추출
        if status_callback:
            await status_callback("내용을 정리하고 있습니다")
        _t = time.monotonic()
        if precomputed_keywords:
            logger.info("[RAG/guide_recommend_v2] 사전 계산된 키워드 사용: %d개", len(precomputed_keywords or []))
            logger.debug("[RAG/guide_recommend_v2] 사전 계산된 키워드 상세: %s", precomputed_keywords)
            gr_triples_list = [filter_okms_keywords(precomputed_keywords)]
            if len(gr_expanded) > 1:
                gr_triples_list.extend([[] for _ in range(len(gr_expanded) - 1)])
        else:
            gr_triples_list = [
                filter_okms_keywords(extract_nouns(eq, use_bigram=False))
                for eq in gr_expanded
            ]
        gr_search_queries = _build_search_queries(list(gr_triples_list))
        logger.info("[TIMING][guide_recommend] StepA-2 키워드 추출 (kiwi): %.3fs", time.monotonic() - _t)
        logger.info(
            f"[RAG/guide_recommend_v2] 키워드 추출 완료: "
            f"검색쿼리 {len(gr_search_queries)}개 (총 {len(gr_triples_list)}개 중)"
        )
        gr_tri_built = []
        for kws in gr_triples_list:
            sq = " ".join(k.strip() for k in kws if k and k.strip())
            gr_tri_built.append(sq)
        for i, sq in enumerate(gr_tri_built, 1):
            if sq:
                logger.debug(f"[RAG/guide_recommend_v2] [키워드검색어] #{i}: {sq}")

        # Step B: Group A — 균등 가중치 듀얼 검색 (병렬)
        # per_query_limit 은 Mariner 한 호출에서 받는 행 수 — 검색식은 동일하고
        # 결과 N만 늘어나 비용이 거의 증가하지 않는 선에서 8로 상향(재귀 트리거 빈도 ↓).
        _GR_GA_PER_QUERY   = 8
        _GR_GA_TOP_N       = 10
        _GR_GOV_OKMS_TOP_N = 3   # GOV_OKMS_V1 독립 쿼터
        if status_callback:
            await status_callback("질문을 분석하고 있습니다")

        def _group_a_run_okms_query(vector: str, keywords: str):
            """Group A 검색: 추천 질의는 탐색 폭 유지를 위해 사업명 앵커를 비활성화한다."""
            try:
                return query_group_a_documents(
                    vector, keywords, selected_collection,
                    year_filters=gr_year_filters or None,
                    sigun_filters=gr_sigun_filters,
                    lifecycle_filter=lifecycle or None,
                    excluded_chunk_ids=excluded_chunk_ids,
                    apply_business_anchor=False,
                )
            except Exception as e:
                logger.warning(f"[RAG/guide_recommend_v2] Group A 쿼리 검색 실패: {e}")
                return [], []

        def _run_gov_okms_query(search_str: str):
            """GOV_OKMS_V1 단일 검색: SIGUN/YEAR 필터 없음, LIFE_CYCLE만 선택 적용"""
            try:
                return query_gov_okms_documents(
                    search_str,
                    collection=Config.RAG_GOV_OKMS_COLLECTION,
                    lifecycle_filter=lifecycle or None,
                    sigun_filters=gr_sigun_filters,
                    excluded_chunk_ids=excluded_chunk_ids,
                )
            except Exception as e:
                logger.warning(f"[RAG/guide_recommend_v2] GOV_OKMS 쿼리 실패: {e}")
                return []

        loop = asyncio.get_event_loop()

        # eq(확장쿼리)와 sq(트리플쿼리)를 쌍으로 Group A 검색 + GOV_OKMS 단일 검색 (동시 병렬)
        _t = time.monotonic()
        gr_group_a_docs, gov_okms_docs = await collect_okms_groupa_and_gov_docs(
            message=message,
            reformed_query=reformed_query,
            policy_priority_tag=precomputed_policy_priority_tag,
            expanded_queries=gr_expanded,
            tri_built=gr_tri_built,
            per_query_limit=_GR_GA_PER_QUERY,
            run_group_a=_group_a_run_okms_query,
            run_gov=_run_gov_okms_query,
            log_prefix="RAG/guide_recommend_v2",
            status_callback=status_callback,
            log_skip_empty_triple=True,
            policy_search_boost_enabled=_query_policy_boost_enabled,
        )
        logger.info("[TIMING][guide_recommend] StepB GroupA+GOV_OKMS 병렬 검색: %.3fs", time.monotonic() - _t)

        # OKMS 독립 정렬 → top 10 (Fallback 입력용)
        gr_group_a_top = sorted(
            _deduplicate_documents(gr_group_a_docs),
            key=lambda x: float(x.get("WEIGHT", 0) or 0),
            reverse=True,
        )[:_GR_GA_TOP_N]
        logger.info(f"[RAG/guide_recommend_v2] [OKMS] 풀: {len(gr_group_a_top)}개 (수집 {len(gr_group_a_docs)}개)")
        for idx, doc in enumerate(gr_group_a_top, 1):
            logger.debug(
                f"[RAG/guide_recommend_v2] [OKMS] #{idx}"
                f"  NAME={doc.get('NAME', '')}"
                f"  WEIGHT={doc.get('WEIGHT', '')}"
                f"  YEAR={doc.get('YEAR', '')}"
                f"  SIGUN={doc.get('SIGUN', '')}"
                f"  CHUNK_ID={doc.get('CHUNK_ID', '')}"
            )

        # GOV_OKMS_V1 독립 정렬 → top 3 (쿼터 확정)
        gov_okms_top_docs = sorted(
            _deduplicate_documents(gov_okms_docs),
            key=lambda x: float(x.get("WEIGHT", 0) or 0),
            reverse=True,
        )[:_GR_GOV_OKMS_TOP_N]
        logger.info(f"[RAG/guide_recommend_v2] [GOV_OKMS] 쿼터 확정: {len(gov_okms_top_docs)}개 (수집 {len(gov_okms_docs)}개)")
        for idx, doc in enumerate(gov_okms_top_docs, 1):
            logger.debug(
                f"[RAG/guide_recommend_v2] [GOV_OKMS] #{idx}"
                f"  NAME={doc.get('NAME', '')}"
                f"  WEIGHT={doc.get('WEIGHT', '')}"
                f"  CHUNK_ID={doc.get('CHUNK_ID', '')}"
            )

        # Step D: OKMS 쿼터 → top 5
        _GR_FINAL_TOP_N = 5
        gr_top_docs = gr_group_a_top[:_GR_FINAL_TOP_N]
        logger.info(f"[RAG/guide_recommend_v2] [OKMS] 쿼터 확정: {len(gr_top_docs)}개")
        for i, doc in enumerate(gr_top_docs, 1):
            logger.debug(
                f"[RAG/guide_recommend_v2] [OKMS] #{i} "
                f"NAME={_get_document_name(doc)}, WEIGHT={doc.get('WEIGHT', '?')}"
            )

        # ====================================================================
        # Step D-F: OKMS Fallback (필터 제거 재검색, 쿼터 미달 시)
        # ====================================================================
        _GR_FALLBACK_THRESHOLD = _GR_FINAL_TOP_N
        if len(gr_top_docs) < _GR_FALLBACK_THRESHOLD:
            logger.info(f"[RAG/guide_recommend_v2] OKMS 결과 {len(gr_top_docs)}건 < {_GR_FALLBACK_THRESHOLD}건 → Fallback 검색 시작")

            def _group_a_run_okms_fallback(vector: str, keywords: str):
                """Group A Fallback: 정상 검색 함수 재사용, 생애주기/사업명 앵커 비활성(시군/연도/제외 유지)"""
                try:
                    return query_group_a_documents(
                        vector, keywords, selected_collection,
                        year_filters=gr_year_filters or None,
                        sigun_filters=gr_sigun_filters,
                        lifecycle_filter=None,
                        excluded_chunk_ids=excluded_chunk_ids,
                        apply_business_anchor=False,
                    )
                except Exception as e:
                    logger.warning(f"[RAG/guide_recommend_v2] Group A Fallback 검색 실패: {e}")
                    return [], []

            _t = time.monotonic()
            gr_fb_a_docs = await collect_okms_groupa_fallback_docs(
                message=message,
                reformed_query=reformed_query,
                expanded_queries=gr_expanded,
                tri_built=gr_tri_built,
                per_query_limit=_GR_GA_PER_QUERY,
                run_group_a_fallback=_group_a_run_okms_fallback,
            )
            logger.info("[TIMING][guide_recommend] StepD-F OKMS Fallback 검색(병렬): %.3fs", time.monotonic() - _t)

            # 기존 결과 + Fallback 결과 합산 → 중복 제거 → top 5
            gr_top_docs = sorted(
                _deduplicate_documents(gr_top_docs + gr_fb_a_docs),
                key=lambda x: float(x.get("WEIGHT", 0) or 0),
                reverse=True,
            )[:_GR_FINAL_TOP_N]
            logger.info(f"[RAG/guide_recommend_v2] OKMS Fallback 후: {len(gr_top_docs)}개 (FB-A {len(gr_fb_a_docs)}개 추가)")

        # OKMS 결과 정렬 (OKMS 쿼터 내) — reserve pool 도 함께 보존
        # top 5 외 잔여 후보(rank 6~)는 재귀 보강 직전에 무료 재활용 대상으로 보관.
        _okms_pool_sorted = sorted(
            _deduplicate_documents(gr_top_docs),
            key=lambda x: float(x.get("WEIGHT", 0) or 0),
            reverse=True,
        )
        gr_top_docs = _okms_pool_sorted[:_GR_FINAL_TOP_N]
        _okms_reserve_pool: List[Dict[str, Any]] = _okms_pool_sorted[_GR_FINAL_TOP_N:]

        # GOV_OKMS_V1 쿼터 추가 (OKMS와 독립, 뒤에 병합)
        gr_top_docs = gr_top_docs + gov_okms_top_docs
        logger.info(
            f"[RAG/guide_recommend_v2] 최종 문서: {len(gr_top_docs)}개 "
            f"(OKMS {_GR_FINAL_TOP_N}개 쿼터 + GOV_OKMS {len(gov_okms_top_docs)}개 쿼터)"
        )

        # Step D-1: LLM 관련성 필터 (무관 문서 제거) — 비활성화
        if status_callback:
            await status_callback("검색 결과를 검증하고 있습니다")
        _t = time.monotonic()
        gr_top_docs = apply_policy_priority_to_documents(
            precomputed_policy_priority_tag,
            gr_top_docs,
            log_prefix="[RAG/guide_recommend_v2]",
            apply_enabled=not _skip_policy_boost,
        )
        # 관련성 필터 입력 chunk_id 스냅샷 — 거절된 문서를 재귀 보강 시 재탐색에서 제외
        _pre_filter_chunk_ids = [d.get("CHUNK_ID") for d in gr_top_docs if d.get("CHUNK_ID")]
        gr_top_docs = await filter_irrelevant_docs(reformed_query, gr_top_docs, sigun_filters=gr_sigun_filters)
        logger.info("[TIMING][guide_recommend] StepD-1 관련성 필터 [8b/sllm]: %.3fs", time.monotonic() - _t)
        logger.info(f"[RAG/guide_recommend_v2] 관련성 필터 후: {len(gr_top_docs)}개 문서")

        # D-1 SLM 필터가 전부 제거하면 reserve·재귀 생략 — 컬렉션 자체에 해당 쿼리와
        # 관련된 문서가 없다는 신호이므로 추가 검색해도 의미 없다.
        _d1_filtered_all = not bool(gr_top_docs)
        if _d1_filtered_all:
            logger.info("[RAG/guide_recommend_v2] D-1 필터 후 0건 → reserve·재귀 보강 생략")

        if excluded_chunk_ids or excluded_service_names:
            gr_top_docs = filter_excluded_docs(gr_top_docs, excluded_chunk_ids or [], excluded_service_names)
            logger.info(f"[MoreResults][guide_recommend] 제외 필터 후: {len(gr_top_docs)}개 문서")

        # 관련성 필터에서 거절된 chunk_id (재귀에서 재평가 방지)
        _survived_after_filter = {d.get("CHUNK_ID") for d in gr_top_docs if d.get("CHUNK_ID")}
        rejected_chunk_ids: List[str] = [
            cid for cid in _pre_filter_chunk_ids if cid and cid not in _survived_after_filter
        ]
        if rejected_chunk_ids:
            logger.debug(
                f"[RAG/guide_recommend_v2] 관련성 필터 거절 chunk_id {len(rejected_chunk_ids)}건 추적"
            )

        _GR_TARGET_TOTAL = _GR_FINAL_TOP_N + _GR_GOV_OKMS_TOP_N  # 5 + 3 = 8

        # ====================================================================
        # Step D-1.4: Reserve pool 재활용 (재귀 전, 무료 보강)
        # 이미 검색된 OKMS 후보 중 top 5 외 잔여(_okms_reserve_pool)에서
        # 외부 제외/거절/현재 보유분을 빼고 부족분만 채운다.
        # - Mariner 호출 0, SLM 필터 0 (reserve 는 초기 검색에서 WEIGHT 검증됨)
        # - D-1이 전부 필터링한 경우(컬렉션 전체 비관련)면 생략
        # ====================================================================
        if not _d1_filtered_all and len(gr_top_docs) < _GR_TARGET_TOTAL and _okms_reserve_pool:
            _need = _GR_TARGET_TOTAL - len(gr_top_docs)
            _held_chunk_ids = {d.get("CHUNK_ID") for d in gr_top_docs if d.get("CHUNK_ID")}
            _excl_set = (
                set(excluded_chunk_ids or [])
                | set(rejected_chunk_ids)
                | _held_chunk_ids
            )
            _reserve_candidates = [
                d for d in _okms_reserve_pool
                if d.get("CHUNK_ID") and d.get("CHUNK_ID") not in _excl_set
            ]
            if _reserve_candidates:
                if excluded_chunk_ids or excluded_service_names:
                    _reserve_candidates = filter_excluded_docs(
                        _reserve_candidates, excluded_chunk_ids or [], excluded_service_names
                    )
                _add = _reserve_candidates[:_need]
                if _add:
                    _add = apply_policy_priority_to_documents(
                        precomputed_policy_priority_tag,
                        _add,
                        log_prefix="[RAG/guide_recommend_v2/reserve]",
                        apply_enabled=not _skip_policy_boost,
                    )
                    gr_top_docs = list(gr_top_docs) + _add
                    logger.info(
                        f"[RAG/guide_recommend_v2] reserve 풀에서 {len(_add)}건 보강 → 합산 {len(gr_top_docs)}건"
                    )

        # ====================================================================
        # Step D-1.5: 재귀 보강 (reserve 보강 후에도 8건 미달 시, 최대 3회 추가 검색)
        # - 반복마다 필터를 점진적으로 완화하고 거절된 chunk_id를 누적 제외
        # - SLM 관련성 필터 없음 — iter 내 SLM 호출을 제거해 응답속도 개선
        #   (Mariner WEIGHT + 누적 exclude 로 품질 유지)
        # - D-1이 전부 필터링한 경우(컬렉션 전체 비관련)면 생략
        # ====================================================================
        _GR_RECURSIVE_MAX_ITERS = 3
        if not _d1_filtered_all and len(gr_top_docs) < _GR_TARGET_TOTAL:
            logger.info(
                f"[RAG/guide_recommend_v2] 관련성 필터 후 {len(gr_top_docs)}건 "
                f"< {_GR_TARGET_TOTAL}건 → 재귀 보강 시작 (최대 {_GR_RECURSIVE_MAX_ITERS}회)"
            )

            # 재귀는 전체 확장 쿼리를 사용 (cap 미적용 → 검색 다양성 확보)
            _recur_candidates = precomputed_expanded_queries or gr_expanded
            recur_expanded = dedupe_cap_expanded_queries(
                _recur_candidates or [],
                reformed_query=gr_expand_base,
            )
            recur_tri = [
                " ".join(
                    k.strip()
                    for k in filter_okms_keywords(extract_nouns(eq, use_bigram=False))
                    if k and k.strip()
                )
                for eq in recur_expanded
            ]

            # 반복별 필터 완화 전략: (lifecycle, year, boost)
            _RELAX_PLAN = [
                {"lifecycle": False, "year": True,  "boost": _query_policy_boost_enabled},   # iter1: lifecycle off
                {"lifecycle": False, "year": True,  "boost": False},                          # iter2: boost off
                {"lifecycle": False, "year": False, "boost": False},                          # iter3: year off
            ]

            for _iter in range(1, _GR_RECURSIVE_MAX_ITERS + 1):
                if len(gr_top_docs) >= _GR_TARGET_TOTAL:
                    break

                relax = _RELAX_PLAN[_iter - 1]
                _lc = (lifecycle or None) if relax["lifecycle"] else None
                _yr = (gr_year_filters or None) if relax["year"] else None
                _boost_on = relax["boost"]

                # 누적 제외: 외부 제외 + 보유 중인 문서 + 관련성에서 거절된 문서
                accumulated_excluded = (
                    list(excluded_chunk_ids or [])
                    + [d.get("CHUNK_ID") for d in gr_top_docs if d.get("CHUNK_ID")]
                    + list(rejected_chunk_ids)
                )
                logger.info(
                    f"[RAG/guide_recommend_v2] 재귀 #{_iter} 시작 — lifecycle={_lc!r}, "
                    f"year={_yr!r}, boost={_boost_on}, 제외={len(accumulated_excluded)}건"
                )

                # iter 내 Mariner 실패 집계 — 콜백마다 WARNING 도배되던 것을
                # 한 줄 요약으로 모으고, 타임아웃(-60004) 다수면 후속 iter 를 중단한다.
                _iter_fails: Dict[str, Any] = {
                    "group_a_total": 0,
                    "group_a_timeout": 0,
                    "gov_total": 0,
                    "gov_timeout": 0,
                    "first_error": None,
                }

                def _record_failure(kind: str, exc: Exception, _store=_iter_fails) -> None:
                    msg = str(exc)
                    is_timeout = "-60004" in msg
                    _store[f"{kind}_total"] += 1
                    if is_timeout:
                        _store[f"{kind}_timeout"] += 1
                    if _store["first_error"] is None:
                        _store["first_error"] = msg

                def _recur_group_a(vector: str, keywords: str, _excl=accumulated_excluded, _lc_v=_lc, _yr_v=_yr):
                    try:
                        return query_group_a_documents(
                            vector, keywords, selected_collection,
                            year_filters=_yr_v,
                            sigun_filters=gr_sigun_filters,
                            lifecycle_filter=_lc_v,
                            excluded_chunk_ids=_excl,
                            apply_business_anchor=False,
                        )
                    except Exception as e:
                        _record_failure("group_a", e)
                        return [], []

                def _recur_gov(search_str: str, _excl=accumulated_excluded, _lc_v=_lc):
                    try:
                        return query_gov_okms_documents(
                            search_str,
                            collection=Config.RAG_GOV_OKMS_COLLECTION,
                            lifecycle_filter=_lc_v,
                            sigun_filters=gr_sigun_filters,
                            excluded_chunk_ids=_excl,
                        )
                    except Exception as e:
                        _record_failure("gov", e)
                        return []

                _t_recur = time.monotonic()
                recur_docs = await collect_okms_groupa_and_gov_fallback_docs(
                    message=message,
                    reformed_query=reformed_query,
                    policy_priority_tag=precomputed_policy_priority_tag,
                    expanded_queries=recur_expanded,
                    tri_built=recur_tri,
                    per_query_limit=_GR_GA_PER_QUERY,
                    run_group_a=_recur_group_a,
                    run_gov=_recur_gov,
                    max_policy_pairs=1,
                    policy_search_boost_enabled=_boost_on,
                )
                logger.info(
                    "[TIMING][guide_recommend] StepD-1.5 재귀 #%d 보강검색: %.3fs",
                    _iter, time.monotonic() - _t_recur,
                )

                # 실패 집계 한 줄 로그 + 타임아웃 다수 발생 시 후속 iter 중단
                _total_fail = _iter_fails["group_a_total"] + _iter_fails["gov_total"]
                _total_timeout = _iter_fails["group_a_timeout"] + _iter_fails["gov_timeout"]
                if _total_fail:
                    logger.warning(
                        "[RAG/guide_recommend_v2] 재귀 #%d Mariner 실패 집계: "
                        "총 %d건(timeout=%d) [Group A=%d/timeout=%d, GOV=%d/timeout=%d] 첫 오류: %s",
                        _iter,
                        _total_fail,
                        _total_timeout,
                        _iter_fails["group_a_total"],
                        _iter_fails["group_a_timeout"],
                        _iter_fails["gov_total"],
                        _iter_fails["gov_timeout"],
                        _iter_fails["first_error"],
                    )
                # 타임아웃이 3건 이상이면 다음 iter 가 더 무거워질 가능성이 높아 즉시 종료
                if _total_timeout >= 3:
                    logger.warning(
                        "[RAG/guide_recommend_v2] 재귀 #%d 타임아웃 %d건 → 후속 재귀 중단",
                        _iter, _total_timeout,
                    )
                    break

                # 이미 보유/거절된 chunk_id 제외 + 중복 제거
                exclude_set = set(accumulated_excluded)
                new_docs = _deduplicate_documents([
                    d for d in recur_docs
                    if d.get("CHUNK_ID") and d.get("CHUNK_ID") not in exclude_set
                ])
                logger.info(
                    f"[RAG/guide_recommend_v2] 재귀 #{_iter} 신규 후보: {len(new_docs)}건 "
                    f"(수집 {len(recur_docs)}건)"
                )
                if not new_docs:
                    logger.info(f"[RAG/guide_recommend_v2] 재귀 #{_iter}: 신규 문서 없음 → 다음 반복")
                    continue

                # SLM 관련성 필터 생략 — more_info exclusion 만 적용 후 바로 추가
                if excluded_chunk_ids or excluded_service_names:
                    new_docs = filter_excluded_docs(
                        new_docs, excluded_chunk_ids or [], excluded_service_names
                    )

                if not new_docs:
                    logger.info(f"[RAG/guide_recommend_v2] 재귀 #{_iter}: 제외 필터 후 신규 0건")
                    continue

                new_docs = apply_policy_priority_to_documents(
                    precomputed_policy_priority_tag,
                    new_docs,
                    log_prefix=f"[RAG/guide_recommend_v2/recur#{_iter}]",
                    apply_enabled=not _skip_policy_boost,
                )
                gr_top_docs = list(gr_top_docs) + new_docs
                logger.info(
                    f"[RAG/guide_recommend_v2] 재귀 #{_iter} 합산 후: {len(gr_top_docs)}건 "
                    f"(신규 {len(new_docs)}건 추가)"
                )

            # 8건 초과 시 WEIGHT 기준 상위 8건만 유지
            if len(gr_top_docs) > _GR_TARGET_TOTAL:
                gr_top_docs = sorted(
                    gr_top_docs,
                    key=lambda x: float(x.get("WEIGHT", 0) or 0),
                    reverse=True,
                )[:_GR_TARGET_TOTAL]
                logger.info(f"[RAG/guide_recommend_v2] 재귀 후 상위 {_GR_TARGET_TOTAL}건 캡: {len(gr_top_docs)}건")
            else:
                logger.info(f"[RAG/guide_recommend_v2] 재귀 종료: 최종 {len(gr_top_docs)}건")

        # Step D-2: 웨이트 상위 30% → 최신순 / 나머지 → 웨이트 내림차순
        gr_top_docs = sort_weight_top30_then_year(gr_top_docs)

        # 참고 문서 구성
        gr_referenced_documents = build_referenced_documents(gr_top_docs)

        # 최종 응답 생성
        if status_callback:
            await status_callback("최종답변을 생성하고 있습니다")
        # user_region: 추출된 시군 원본 (LLM 표시용)
        user_region = " ".join(r for r in sigun_raws if r != "경남") or ""
        _t = time.monotonic()
        _final_user_msg = (final_user_message or "").strip() or message
        gr_response = await generate_final_response_v2(
            _final_user_msg, gr_top_docs, temperature, max_tokens, stream,
            frequency_penalty, repetition_penalty, top_p, top_k, seed, tools,
            intent=intent,
            lifecycle=lifecycle,
            messages=messages,
            policy_priority_tag=precomputed_policy_priority_tag,
            user_region=user_region,
            user_birth_year=str(birth_year) if birth_year else "",
            more_info_mode=bool(excluded_chunk_ids or excluded_service_names),
            use_llm_recommended_prompt=recommended_question_prompt,
        )
        logger.info("[TIMING][guide_recommend] 최종 응답 생성 [32b/luxia]: %.3fs", time.monotonic() - _t)
        logger.info("[TIMING][guide_recommend] process_rag_guide_recommend 전체: %.3fs", time.monotonic() - t_total)
        return gr_response, gr_referenced_documents

    except Exception as e:
        logger.error(f"[RAG/guide_recommend_v2] 처리 중 오류: {e}", exc_info=True)
        raise
