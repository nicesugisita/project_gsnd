"""
RAG 서비스 — guide_recommend 전용 플로우

guide_recommend 의도의 검색 및 응답 생성을 별도 파일로 분리한 것입니다.
로직 자체는 기존 services/rag_service.py의 guide_recommend 플로우와 동일합니다.

기존 services/rag_service.py는 변경하지 않습니다.
"""

import logging
import asyncio
import time
from datetime import date
from typing import Dict, Any, List, Optional

from app.core.config import Config
from app.core.constants import GUIDE_RECOMMEND_MAX_TOKENS

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
    _extract_hshd_sttn_from_message,
    _build_search_queries,
    filter_okms_keywords,
)
from .response_generator import generate_final_response_v2
from app.shared.utils.keyword_extractor import extract_nouns
from app.mariner.sigun_utils import normalize_sigun
from app.shared.utils.year_filter import extract_year_filters
from app.chat.infra.rag.rrf_reranker import rerank_by_rrf
from .policy_priority import resolve_policy_boost_keywords
from .variable_count import extract_topic_terms, select_variable_count
from .pipeline_utils import (
    collect_okms_groupa_and_gov_docs,
    collect_okms_groupa_fallback_docs,
    collect_okms_groupa_and_gov_fallback_docs,
    filter_gov_okms_docs_by_lifecycle,
    prioritize_general_household,
)
logger = logging.getLogger(__name__)


def _extract_sigun_from_history(messages: Optional[list]) -> list:
    """대화 히스토리(user 메시지)에서 가장 최근 시군을 역순 스캔. 없으면 [].

    extract_lifecycle_from_history 와 동일한 sticky 패턴 — 새 시군이 안 나온 동안
    직전 시군을 유지하기 위함. (현재 턴 우선은 호출부에서 message 를 먼저 확인.)
    """
    for m in reversed(messages or []):
        if m.get("role") != "user":
            continue
        raws = _extract_sigun_from_message(m.get("content", "") or "")
        if raws:
            return raws
    return []


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
    llm_excluded_services: List[str] = None,
    final_user_message: Optional[str] = None,
    recommended_question_prompt: bool = False,
    precomputed_search_target: Optional[str] = None,
    precomputed_policy_priority_tag: Optional[str] = None,
    precomputed_lifecycle_tags: Optional[List[str]] = None,
    precomputed_household_tags: Optional[List[str]] = None,
    service_target: Optional[str] = "official",
) -> tuple[Any, List[Dict[str, str]]]:
    """
    RAG 문서 검색 및 최종 응답 생성 — guide_recommend 전용

    guide_recommend 의도일 때만 이 함수의 로직을 실행하며,
    그 외 의도는 기존 함수로 위임합니다.
    """
    # guide_recommend가 아닌 경우 기존 함수로 위임
    _ = precomputed_search_target
    _ = service_target  # guide_recommend는 GSND 미사용 — 시그니처 호환 위해 받기만 함

    try:
        t_total = time.monotonic()
        _skip_policy_boost = bool(excluded_chunk_ids or excluded_service_names)
        # LLM 선별 모드: 룰베이스 개수 결정(reserve/재귀/가변개수)을 끄고 RRF 상위 N건을
        # 그대로 최종응답 LLM에 넘긴다(관련 문서 선별은 선별 프롬프트가 담당).
        # more_info 후속(excluded 존재)은 '더 보기' 의미라 이 모드에서 제외(기존 흐름 유지).
        _llm_select = not _skip_policy_boost
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
            # 시군: 현재 message 우선(새 시군이면 즉시 교체) → 없으면 history 역순 스캔으로
            # 직전 시군 유지(sticky) → 그래도 없으면 reformed_query 보강.
            sigun_raws = (
                _extract_sigun_from_message(message)
                or _extract_sigun_from_history(messages)
                or _extract_sigun_from_message(reformed_query)
            )
            logger.debug(f"[RAG/guide_recommend_v2] 추출된 시군: {sigun_raws}")
            _gr_normalized = [normalize_sigun(r) for r in sigun_raws if r != "경남"]
            _gr_city_filters = [s for s in _gr_normalized if s.startswith("경상남도 ")]
            gr_sigun_filters = list(dict.fromkeys(_gr_city_filters)) if _gr_city_filters else []
        logger.debug(f"[RAG/guide_recommend_v2] sigun 필터: {gr_sigun_filters}")

        birth_year = _extract_birth_year_from_message(message)

        # 생애주기: rewrite 프롬프트(LLM 멀티태그)면 precomputed 사용 — 룰 기반 추출을 완전 대체.
        # 빈 리스트면 'LLM이 태그 없음'(부모 나이로 추정 금지). precomputed 가 None(비-rewrite/구
        # 프롬프트)일 때만 기존 룰 기반 단일 추출로 폴백한다.
        if precomputed_lifecycle_tags is not None:
            lifecycle_tags = list(precomputed_lifecycle_tags)
            logger.debug(f"[RAG/guide_recommend_v2] LLM 생애주기 태그: {lifecycle_tags}")
        else:
            if birth_year:
                _lc = _birth_year_to_lifecycle(birth_year)
                logger.debug(f"[RAG/guide_recommend_v2] 출생연도: {birth_year} → 생애주기: '{_lc}'")
            else:
                # sticky: 현재 message 우선 → history 역순 → reformed_query 보강.
                from app.chat.lifecycle import extract_lifecycle_from_history
                _lc = (
                    _extract_lifecycle_from_message(message)
                    or extract_lifecycle_from_history(messages or [])
                    or _extract_lifecycle_from_message(reformed_query)
                )
                if _lc:
                    logger.debug(f"[RAG/guide_recommend_v2] 생애주기 추출(현재→history→reformed): '{_lc}'")
                else:
                    logger.debug(f"[RAG/guide_recommend_v2] 생애주기 단서 없음 → 필터 미적용")
            lifecycle_tags = [_lc] if _lc else []

        # 가구상황: rewrite 분류기(precomputed)면 LLM 멀티태그 사용 — 룰 추출 대체.
        # 특정계층(저소득/장애인/한부모·조손/다문화·탈북민/다자녀/보훈)이 있으면 그 리스트로
        # 소프트 부스트, 없으면 '일반가구'(특정계층 미해당). None(분류기 실패)이면 룰 폴백.
        if precomputed_household_tags is not None:
            _hh_specifics = [t for t in precomputed_household_tags if t and t != "일반가구"]
            gr_hshd_sttn = _hh_specifics if _hh_specifics else "일반가구"
            gr_hshd_synonyms = []  # 캐노니컬 카테고리명이라 동의어 확장 불필요
            logger.debug(
                f"[RAG/guide_recommend_v2] LLM 가구상황 태그: {precomputed_household_tags} → filter={gr_hshd_sttn!r}"
            )
        else:
            # (구 프롬프트/분류기 실패 폴백) message+reformed_query 결합 텍스트에서 룰 추출.
            gr_hshd_sttn, gr_hshd_synonyms = _extract_hshd_sttn_from_message(f"{message} {reformed_query}")
            if gr_hshd_sttn:
                logger.debug(
                    f"[RAG/guide_recommend_v2] 가구상황 추출(룰): '{gr_hshd_sttn}' synonyms={gr_hshd_synonyms}"
                )

        # 연도 필터: guide_recommend 는 항상 현재 연도 문서만 추천 (timeliness 보장).
        # 사용자가 과거/미래 연도를 명시해도 추천 결과는 현재 연도로 강제.
        _current_year = str(date.today().year)
        _user_specified_years = extract_year_filters(message)
        if _user_specified_years and _user_specified_years != [_current_year]:
            logger.info(
                "[RAG/guide_recommend_v2] 사용자 명시 연도=%s → 현재 연도 [%s] 강제 (guide_recommend timeliness)",
                _user_specified_years, _current_year,
            )
        gr_year_filters = [_current_year]
        logger.debug(f"[RAG/guide_recommend_v2] 연도 필터: {gr_year_filters}")

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
        # 관련성 필터(D-1)·일반가구/생애주기 후처리(D-1.7)가 후보를 크게 줄이므로,
        # 후처리 후 8건을 확보하려면 후보 풀을 넓혀야 한다(재귀 완화로 메우면 비적합 유입).
        # _GR_GA_MAX_RESULTS: Mariner 한 쿼리 반환 행 수(전역 MARINER_MAX_RESULTS=5 를
        # guide_recommend 한정 상향). per_query_limit·TOP_N 도 함께 올려 필터 입력을 키운다.
        _GR_GA_MAX_RESULTS = 15
        _GR_GA_PER_QUERY   = 15
        _GR_GA_TOP_N       = 15
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
                    lifecycle_filter=lifecycle_tags or None,
                    hshd_sttn_filter=gr_hshd_sttn or None,
                    hshd_sttn_synonyms=gr_hshd_synonyms or None,
                    excluded_chunk_ids=excluded_chunk_ids,
                    excluded_business_keywords=llm_excluded_services,
                    apply_business_anchor=False,
                    max_results=_GR_GA_MAX_RESULTS,
                )
            except Exception as e:
                logger.warning(f"[RAG/guide_recommend_v2] Group A 쿼리 검색 실패: {e}")
                return [], []

        def _run_gov_okms_query(search_str: str):
            """GOV_OKMS_V1 단일 검색: SIGUN/YEAR 필터 없음, LIFE_CYCLE/HOUSE_SITUATION 선택 적용"""
            try:
                return query_gov_okms_documents(
                    search_str,
                    collection=Config.RAG_GOV_OKMS_COLLECTION,
                    lifecycle_filter=lifecycle_tags or None,
                    sigun_filters=gr_sigun_filters,
                    excluded_chunk_ids=excluded_chunk_ids,
                    excluded_business_keywords=llm_excluded_services,
                    hshd_sttn_filter=gr_hshd_sttn or None,
                    hshd_sttn_synonyms=gr_hshd_synonyms or None,
                    max_results=_GR_GA_MAX_RESULTS,
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
        # GOV_OKMS WHERE 의 LIFE_CYCLE 필터(op 34)가 일관적이지 않아 불일치 문서가 통과하는
        # 사례 확인됨 → 결정적 post-filter 로 보강. 사용자 lifecycle 없으면 필터 안 함.
        _gov_okms_pool = _deduplicate_documents(gov_okms_docs)
        _gov_okms_pool = filter_gov_okms_docs_by_lifecycle(_gov_okms_pool, lifecycle_tags)
        # B1: GOV 쿼터(3)보다 큰 후보 풀을 유지한다 — off-topic GOV 가 관련성 필터에 떨려도
        # 생존분에서 3건을 확보하기 위함. 쿼터 cap 은 필터 뒤로 미룬다.
        gov_okms_candidates = sorted(
            _gov_okms_pool,
            key=lambda x: float(x.get("WEIGHT", 0) or 0),
            reverse=True,
        )
        logger.info(f"[RAG/guide_recommend_v2] [GOV_OKMS] 후보: {len(gov_okms_candidates)}개 (수집 {len(gov_okms_docs)}개, 쿼터 {_GR_GOV_OKMS_TOP_N})")
        for idx, doc in enumerate(gov_okms_candidates, 1):
            logger.debug(
                f"[RAG/guide_recommend_v2] [GOV_OKMS] #{idx}"
                f"  NAME={doc.get('NAME', '')}"
                f"  WEIGHT={doc.get('WEIGHT', '')}"
                f"  CHUNK_ID={doc.get('CHUNK_ID', '')}"
            )

        # Step D: OKMS 쿼터 → top N
        _GR_FINAL_TOP_N = 8
        # B1: 쿼터(5)보다 큰 OKMS 후보 풀(최대 _GR_GA_TOP_N=10)을 필터에 태운다. cap 은 필터 뒤로.
        gr_top_docs = list(gr_group_a_top)
        logger.info(f"[RAG/guide_recommend_v2] [OKMS] 후보: {len(gr_top_docs)}개 (쿼터 {_GR_FINAL_TOP_N})")
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
                        # 소프트부스트라 recall 을 줄이지 않으므로 fallback 에서도 일반가구 부스트 유지
                        hshd_sttn_filter=gr_hshd_sttn or None,
                        hshd_sttn_synonyms=gr_hshd_synonyms or None,
                        excluded_chunk_ids=excluded_chunk_ids,
                        excluded_business_keywords=llm_excluded_services,
                        apply_business_anchor=False,
                        max_results=_GR_GA_MAX_RESULTS,
                    )
                except Exception as e:
                    logger.warning(f"[RAG/guide_recommend_v2] Group A Fallback 검색 실패: {e}")
                    return [], []

            _t = time.monotonic()
            gr_fb_a_docs = await collect_okms_groupa_fallback_docs(
                message=message,
                reformed_query=reformed_query,
                policy_priority_tag=precomputed_policy_priority_tag,
                expanded_queries=gr_expanded,
                tri_built=gr_tri_built,
                per_query_limit=_GR_GA_PER_QUERY,
                run_group_a_fallback=_group_a_run_okms_fallback,
            )
            logger.info("[TIMING][guide_recommend] StepD-F OKMS Fallback 검색(병렬): %.3fs", time.monotonic() - _t)

            # 기존 후보 + Fallback 후보 합산 → 중복 제거 (B1: 여기선 cap 하지 않음 — 필터 뒤 cap)
            gr_top_docs = sorted(
                _deduplicate_documents(gr_top_docs + gr_fb_a_docs),
                key=lambda x: float(x.get("WEIGHT", 0) or 0),
                reverse=True,
            )
            logger.info(f"[RAG/guide_recommend_v2] OKMS Fallback 후 후보: {len(gr_top_docs)}개 (FB-A {len(gr_fb_a_docs)}개 추가)")

        # ====================================================================
        # Step D-1 (B1): 후보 병합 → 정책부스트 → 관련성 필터 → 풀별 쿼터 cap
        # 필터를 큰 후보풀(OKMS≤10 + GOV 수집분)에 먼저 적용하고 cap 을 뒤로 미뤄,
        # 필터가 일부를 떨궈도 생존분에서 쿼터(OKMS 5 + GOV 3)를 채운다.
        # ====================================================================
        _gov_cand_ids = {d.get("CHUNK_ID") for d in gov_okms_candidates if d.get("CHUNK_ID")}
        _merged_candidates = list(gr_top_docs) + list(gov_okms_candidates)

        if status_callback:
            await status_callback("검색 결과를 검증하고 있습니다")
        _t = time.monotonic()
        _merged_candidates = apply_policy_priority_to_documents(
            precomputed_policy_priority_tag,
            _merged_candidates,
            log_prefix="[RAG/guide_recommend_v2]",
            apply_enabled=not _skip_policy_boost,
        )
        # 관련성 필터 입력 chunk_id 스냅샷 — 거절된 문서를 재귀 보강 시 재탐색에서 제외
        _pre_filter_chunk_ids = [d.get("CHUNK_ID") for d in _merged_candidates if d.get("CHUNK_ID")]
        # [단계 진단] 리랭킹 전 후보풀 (관련성 필터·RRF 융합 직전)
        try:
            from app.chat.infra.rag.stage_trace import record_docs as _stage_rec_docs
            _stage_rec_docs("rerank_before", _merged_candidates)
        except Exception:  # noqa: BLE001
            pass
        _survivors = _merged_candidates

        # 후보 풀을 출처별로 분리 (GOV 는 CHUNK_ID 로 식별, 나머지는 OKMS).
        _gov_surv = [d for d in _survivors if d.get("CHUNK_ID") in _gov_cand_ids]
        _okms_surv = [d for d in _survivors if d.get("CHUNK_ID") not in _gov_cand_ids]
        # 쿼터 concat 대신 두 풀을 rank 기반 RRF 융합 (출처 간 WEIGHT 스케일 편향 제거).
        # 쿼터(OKMS 5 + GOV 3) 미보장 — 융합 순위 상위 건 선택, 나머지는 reserve.
        # RRF 는 입력 리스트가 정렬돼 있다고 가정하므로 풀별 WEIGHT 사전 정렬.
        _okms_sorted = sorted(_okms_surv, key=lambda x: float(x.get("WEIGHT", 0) or 0), reverse=True)
        _gov_sorted = sorted(_gov_surv, key=lambda x: float(x.get("WEIGHT", 0) or 0), reverse=True)
        _fused = rerank_by_rrf(_okms_sorted, _gov_sorted)
        # LLM 선별 모드: RRF 상위 GUIDE_LLM_SELECT_MAX_DOCS 건을 그대로 LLM에 전달.
        _cap = Config.GUIDE_LLM_SELECT_MAX_DOCS if _llm_select else (_GR_FINAL_TOP_N + _GR_GOV_OKMS_TOP_N)
        gr_top_docs = _fused[:_cap]
        _okms_reserve_pool: List[Dict[str, Any]] = _fused[_cap:]
        _gov_in_top = sum(1 for d in gr_top_docs if d.get("CHUNK_ID") in _gov_cand_ids)
        logger.info(
            f"[RAG/guide_recommend_v2] 관련성 필터 후 RRF 융합: {len(gr_top_docs)}개 "
            f"(OKMS {len(gr_top_docs) - _gov_in_top} + GOV {_gov_in_top} | 쿼터 미보장 "
            f"| 생존 OKMS={len(_okms_surv)} GOV={len(_gov_surv)} reserve={len(_okms_reserve_pool)})"
        )

        # [단계 진단] 리랭킹 후 (관련성 필터 생존분 → RRF 융합/쿼터 cap 결과)
        try:
            from app.chat.infra.rag.stage_trace import record_docs as _stage_rec_docs
            _stage_rec_docs("rerank_after", gr_top_docs)
        except Exception:  # noqa: BLE001
            pass

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

        # 가변 개수(D-1.8)가 적용될 턴에는 "11까지 채움"용 reserve/재귀 보강이 무의미하고,
        # 재귀 보강(D-1.5)의 타임아웃 조기중단은 부하 따라 후보 풀을 흔드는 비결정성 원천이다.
        # 따라서 가변 개수 활성 + non-more_info 턴이면 보강을 건너뛰어 개수를 결정적으로 만든다.
        # (more_info 후속은 가변 컷에서 제외되므로 보강을 유지해 '더' 결과를 채운다.)
        _variable_count_active = not (excluded_chunk_ids or excluded_service_names)

        # ====================================================================
        # Step D-1.4: Reserve pool 재활용 (재귀 전, 무료 보강)
        # 이미 검색된 OKMS 후보 중 top 5 외 잔여(_okms_reserve_pool)에서
        # 외부 제외/거절/현재 보유분을 빼고 부족분만 채운다.
        # - Mariner 호출 0, SLM 필터 0 (reserve 는 초기 검색에서 WEIGHT 검증됨)
        # - D-1이 전부 필터링한 경우(컬렉션 전체 비관련)면 생략
        # ====================================================================
        if not _variable_count_active and not _llm_select and not _d1_filtered_all and len(gr_top_docs) < _GR_TARGET_TOTAL and _okms_reserve_pool:
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
        _recur_added = False  # 재귀로 SLM 미검증 신규 문서가 추가됐는지 추적 (D-1.6 게이트)
        if not _variable_count_active and not _llm_select and not _d1_filtered_all and len(gr_top_docs) < _GR_TARGET_TOTAL:
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
            # NOTE: guide_recommend 의 연도 필터는 정책상 항상 현재 연도 유지 (timeliness).
            # 따라서 iter3 에서도 year 완화하지 않는다 — 과거 연도 문서가 재귀 보강 결과로
            # 유입되면 "현재 연도 추천" 의도와 충돌.
            _RELAX_PLAN = [
                {"lifecycle": False, "year": True, "boost": _query_policy_boost_enabled},   # iter1: lifecycle off
                {"lifecycle": False, "year": True, "boost": False},                          # iter2: boost off
                {"lifecycle": False, "year": True, "boost": False},                          # iter3: lifecycle/boost 외 완화 없음
            ]

            for _iter in range(1, _GR_RECURSIVE_MAX_ITERS + 1):
                if len(gr_top_docs) >= _GR_TARGET_TOTAL:
                    break

                relax = _RELAX_PLAN[_iter - 1]
                _lc = (lifecycle_tags or None) if relax["lifecycle"] else None
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
                            hshd_sttn_filter=gr_hshd_sttn or None,
                            hshd_sttn_synonyms=gr_hshd_synonyms or None,
                            excluded_chunk_ids=_excl,
                            excluded_business_keywords=llm_excluded_services,
                            apply_business_anchor=False,
                            max_results=_GR_GA_MAX_RESULTS,
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
                            excluded_business_keywords=llm_excluded_services,
                            hshd_sttn_filter=gr_hshd_sttn or None,
                            hshd_sttn_synonyms=gr_hshd_synonyms or None,
                            max_results=_GR_GA_MAX_RESULTS,
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
                _recur_added = True
                logger.info(
                    f"[RAG/guide_recommend_v2] 재귀 #{_iter} 합산 후: {len(gr_top_docs)}건 "
                    f"(신규 {len(new_docs)}건 추가)"
                )

            # 8건 초과 시 상위 8건만 유지 — 융합 순위(rrf_score) 보존
            # (재귀로 추가된 미융합 문서는 rrf_score 없음(0)이라 후순위)
            if len(gr_top_docs) > _GR_TARGET_TOTAL:
                gr_top_docs = sorted(
                    gr_top_docs,
                    key=lambda x: float(x.get("rrf_score", 0.0) or 0.0),
                    reverse=True,
                )[:_GR_TARGET_TOTAL]
                logger.info(f"[RAG/guide_recommend_v2] 재귀 후 상위 {_GR_TARGET_TOTAL}건 캡: {len(gr_top_docs)}건")
            else:
                logger.info(f"[RAG/guide_recommend_v2] 재귀 종료: 최종 {len(gr_top_docs)}건")

        # Step D-1.7: 가구상황·생애주기 적합 후처리
        # - 기본(일반가구 미명시) 질의: 저소득/다문화·탈북민 등 특정계층 '단독' 태그 제도 제외.
        # - 생애주기 추출 시: 질의 생애주기(예 '아동')를 LIFE_CYCLE 에 포함하지 않는 문서 제외
        #   (fallback·재귀가 쿼터 채우려 lifecycle 필터를 풀어 인접 생애주기('청소년' 단독 등)를
        #    끌어오는 누수를 응답 직전에 차단). 둘 다 부족하면 min_keep 까지 WEIGHT 상위로 백필.
        # 명시 가구상황 질의(gr_hshd_sttn != '일반가구')면 가구상황 제외는 건너뛰고 생애주기만 적용.
        _GR_GENERAL_MIN_KEEP = 5
        _gr_require_general = (gr_hshd_sttn == "일반가구")
        # 리랭킹 후처리(D-1.7/D-1.75) 적용 여부:
        # - 기본(플래그 OFF) + LLM 선별 경로 → 후처리 제거(RRF 상위 N건을 그대로 LLM 선별에 위임).
        # - more_info 등 비-LLM선별(레거시) 경로 → 기존 후처리 유지.
        # - 플래그 ON → 모든 경로에서 기존 후처리 복원.
        _apply_post_rerank_filter = Config.GUIDE_POST_RERANK_FILTER_ENABLED or not _llm_select
        if _apply_post_rerank_filter and gr_top_docs and (_gr_require_general or lifecycle_tags):
            # LLM 선별 모드: 'RRF 상위 N건만' 보장 — _survivors 로 풀을 다시 키우지 않고
            # 현재 gr_top_docs(=RRF 상위)만 생애주기/가구 하드필터에 태운다. target 도 cap 동일.
            if _llm_select:
                _pool = list(gr_top_docs)
                _pp_target = Config.GUIDE_LLM_SELECT_MAX_DOCS
            else:
                _pool = _deduplicate_documents(list(gr_top_docs) + list(_survivors))
                _pp_target = _GR_TARGET_TOTAL
            if excluded_chunk_ids or excluded_service_names:
                _pool = filter_excluded_docs(_pool, excluded_chunk_ids or [], excluded_service_names)
            _before = len(gr_top_docs)
            # LLM 선별 모드: 유지 대상은 '생애주기 가드'뿐. 가구상황 require_general 은 끈다
            # (예: '저소득' 태그 임플란트 시술비 지원사업이 '일반가구' 강제로 LLM 전에 탈락하는 누수 방지).
            _pp_require_general = _gr_require_general and not _llm_select
            gr_top_docs = prioritize_general_household(
                _pool, target=_pp_target, min_keep=_GR_GENERAL_MIN_KEEP,
                require_general=_pp_require_general, lifecycle=lifecycle_tags or None,
                # LLM 선별 모드: 생애주기 태그 불일치만으로 명백 관련 문서를 LLM 전에
                # 하드드롭하지 않도록 demote-not-drop(적합 우선 + target 까지 백필).
                backfill_to_target=_llm_select,
            )
            logger.info(
                f"[RAG/guide_recommend_v2] 적합 후처리: {_before} → {len(gr_top_docs)}건 "
                f"(require_general={_gr_require_general}, lifecycle={lifecycle_tags or None})"
            )

        # Step D-1.75 (LLM 선별 모드 + 정책 태그 질의 전용): 주제어 결정적 관련성 게이트.
        # 정책 태그가 있는 '좁은 제도' 질의(임플란트·기초연금 등)에만 적용 — 태그의 큐레이션된
        # 키워드(+동의어)로 주제 무관 문서를 LLM 전에 컷한다. 태그 없는 대상/영역 질의(대학생·노인 등)는
        # 게이트를 걸지 않고 완화 선별 프롬프트(recall)에 맡긴다(과잉컷 방지). 환각도 원천 차단.
        # 전멸 방지: 매칭 0건이면 미적용(전량 유지).
        # 전용 플래그 GUIDE_TOPIC_GATE_ENABLED(기본 False)로 독립 제어 — 기본은 비활성.
        if Config.GUIDE_TOPIC_GATE_ENABLED and _llm_select and precomputed_policy_priority_tag and gr_top_docs:
            from .variable_count import extract_topic_terms as _extract_topic_terms, topical_hit
            _gate_tags, _gate_boost_kw = resolve_policy_boost_keywords(precomputed_policy_priority_tag)
            # 태그 키워드 동의어 갭 보강(예: implant 키워드에 틀니·의치보철 누락).
            _GATE_SYNONYMS = {"implant": ("틀니", "의치", "의치보철")}
            _extra_syn = [s for t in _gate_tags for s in _GATE_SYNONYMS.get(t, ())]
            _gate_kw = precomputed_keywords or extract_nouns(reformed_query)
            _gate_terms = list(dict.fromkeys(
                list(_gate_boost_kw) + _extra_syn + _extract_topic_terms(_gate_kw, exclude=sigun_raws)
            ))
            if _gate_terms:
                _gate_hits = [d for d in gr_top_docs if topical_hit(d, _gate_terms) > 0]
                if _gate_hits:  # 1건이라도 맞을 때만 컷 (전멸 방지)
                    logger.info(
                        "[RAG/guide_recommend_v2] 주제어 게이트(태그=%s): %d → %d건 (terms=%s)",
                        precomputed_policy_priority_tag, len(gr_top_docs), len(_gate_hits), _gate_terms,
                    )
                    gr_top_docs = _gate_hits
                else:
                    logger.info(
                        "[RAG/guide_recommend_v2] 주제어 게이트: 매칭 0건 → 미적용(전량 유지, terms=%s)",
                        _gate_terms,
                    )

        # Step D-1.8: 가변 개수 정책 — 고정 top-N(항상 8~11 채움) 대신 주제어 존재 +
        # 점수 임계로 노출 개수를 가변화. 관련 풀이 작으면 적게, 크면 많이.
        # (more_info 후속은 사용자가 "더"를 명시한 추가요청이라 가변 컷에서 제외 — 풀 그대로.)
        if _variable_count_active and not _llm_select and gr_top_docs:
            _vc_keywords = precomputed_keywords or extract_nouns(reformed_query)
            _topic_terms = extract_topic_terms(_vc_keywords, exclude=sigun_raws)
            _vc_before = len(gr_top_docs)
            gr_top_docs = select_variable_count(
                gr_top_docs, _topic_terms,
                keep_ratio=Config.GUIDE_KEEP_RATIO,
                gap_drop=Config.GUIDE_GAP_DROP,
                min_results=Config.GUIDE_MIN_RESULTS,
                max_results=Config.GUIDE_MAX_RESULTS,
            )
            logger.info(
                "[RAG/guide_recommend_v2] 가변 개수: %d → %d건 (topic_terms=%s)",
                _vc_before, len(gr_top_docs), _topic_terms,
            )

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
        # 카드 포맷이 잘리지 않도록 max_tokens 하한선 적용 (요청값 < 임계 또는 None이면 끌어올림)
        _gr_max_tokens = max(max_tokens or 0, GUIDE_RECOMMEND_MAX_TOKENS)
        if _gr_max_tokens != max_tokens:
            logger.info(
                "[guide_recommend] max_tokens floor 적용: %s → %s",
                max_tokens, _gr_max_tokens,
            )
        gr_response = await generate_final_response_v2(
            _final_user_msg, gr_top_docs, temperature, _gr_max_tokens, stream,
            frequency_penalty, repetition_penalty, top_p, top_k, seed, tools,
            intent=intent,
            lifecycle=", ".join(lifecycle_tags) if lifecycle_tags else "",
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
