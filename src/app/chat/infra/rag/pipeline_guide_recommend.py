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

# TEST_OKMS_V4 Mariner 쿼리셋
from app.mariner.queryset_okms import query_group_a_documents
from app.mariner.queryset_gov_okms import query_gov_okms_documents
from app.mariner.queryset_okms_fallback import query_group_a_fallback
from .common import (
    sort_weight_top30_then_year,
    build_referenced_documents,
    run_welfare_tel_queries,
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
from app.chat.retrieval_judgment import retrieval_sufficiency_judgment
from app.chat.routing import expand_query
from app.shared.utils.keyword_extractor import extract_nouns
from app.mariner.sigun_utils import normalize_sigun
from app.shared.utils.year_filter import extract_year_filters
from app.shared.utils.relevance_filter import filter_irrelevant_docs
from .pipeline_utils import (
    collect_okms_groupa_and_gov_docs,
    collect_okms_groupa_fallback_docs,
    collect_okms_groupa_and_gov_fallback_docs,
    resolve_fallback_max_expanded_queries,
    run_sufficiency_judgment_fast,
    should_rerun_sufficiency_judgment,
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
) -> tuple[Any, List[Dict[str, str]]]:
    """
    RAG 문서 검색 및 최종 응답 생성 — guide_recommend 전용

    guide_recommend 의도일 때만 이 함수의 로직을 실행하며,
    그 외 의도는 기존 함수로 위임합니다.
    """
    # guide_recommend가 아닌 경우 기존 함수로 위임

    try:
        t_total = time.monotonic()
        _GR_FALLBACK_MAX_EXPANDED = resolve_fallback_max_expanded_queries(message)
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
        _GR_GA_PER_QUERY   = 5
        _GR_GA_TOP_N       = 10
        _GR_GOV_OKMS_TOP_N = 3   # GOV_OKMS_V1 독립 쿼터
        if status_callback:
            await status_callback("질문을 분석하고 있습니다")

        def _group_a_run_okms_query(vector: str, keywords: str):
            """Group A 검색: vector/keyword를 균등 가중치로 Mariner에 전송, (keyword_docs, vector_docs) 반환"""
            try:
                return query_group_a_documents(
                    vector, keywords, selected_collection,
                    year_filters=gr_year_filters or None,
                    sigun_filters=gr_sigun_filters,
                    lifecycle_filter=lifecycle or None,
                    excluded_chunk_ids=excluded_chunk_ids,
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
            expanded_queries=gr_expanded,
            tri_built=gr_tri_built,
            per_query_limit=_GR_GA_PER_QUERY,
            run_group_a=_group_a_run_okms_query,
            run_gov=_run_gov_okms_query,
            log_prefix="RAG/guide_recommend_v2",
            status_callback=status_callback,
            log_skip_empty_triple=True,
        )
        logger.info("[TIMING][guide_recommend] StepB GroupA+GOV_OKMS 병렬 검색: %.3fs", time.monotonic() - _t)

        # TEST_OKMS_V4 독립 정렬 → top 10 (Fallback 입력용)
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

        # Step D: TEST_OKMS_V4 쿼터 → top 5
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
                """Group A Fallback: 연도/생애주기 제거, 시군 유지"""
                try:
                    return query_group_a_fallback(
                        vector, keywords, selected_collection,
                        sigun_filters=gr_sigun_filters,
                        excluded_chunk_ids=excluded_chunk_ids,
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

        # ====================================================================
        # Step D-S: OKMS → WELFARE_TEL 전환 적합성 판단 (LLM)
        # ====================================================================
        _t = time.monotonic()
        okms_sufficiency = await run_sufficiency_judgment_fast(
            judge_fn=retrieval_sufficiency_judgment,
            user_question=message,
            intent=intent,
            collection_name=Config.RAG_OKMS_COLLECTION,
            docs=gr_top_docs,
            log_prefix="RAG/guide_recommend_v2",
        )
        logger.info("[TIMING][guide_recommend] StepD-S OKMS 적합성 판단 [8b/sllm]: %.3fs", time.monotonic() - _t)
        logger.info(
            f"[RAG/guide_recommend_v2] OKMS 적합성: sufficient={okms_sufficiency['sufficient']}, "
            f"reason={okms_sufficiency['reason']}"
        )

        # ====================================================================
        # Step D-W: WELFARE_TEL 검색 (OKMS BUSINESS_NAME + SIGUN 기반, OKMS 부족 시)
        # ====================================================================
        _GR_WELFARE_TEL_PER_QUERY = 5
        gr_welfare_tel_docs: List[Dict[str, Any]] = []

        if okms_sufficiency["sufficient"]:
            logger.info("[RAG/guide_recommend_v2] OKMS 결과 충분 — OUR_REGION_TEL 검색 생략")
        else:
            # 2차 fallback: 확장 쿼리 기반 OKMS 보강 검색
            if status_callback:
                await status_callback("검색을 보강하고 있습니다")
            _t = time.monotonic()
            if precomputed_expanded_queries:
                _candidates = precomputed_expanded_queries
                logger.info("[RAG/guide_recommend_v2] fallback: 사전 계산 확장 쿼리 사용")
            else:
                _candidates = await expand_query(gr_expand_base)
            logger.info("[TIMING][guide_recommend] StepD-S2 fallback 쿼리확장: %.3fs", time.monotonic() - _t)
            fallback_expanded = dedupe_cap_expanded_queries(
                _candidates or [], max_n=_GR_FALLBACK_MAX_EXPANDED, reformed_query=gr_expand_base
            )
            if fallback_expanded:
                _t = time.monotonic()
                fallback_tri = [
                    " ".join(
                        k.strip()
                        for k in filter_okms_keywords(extract_nouns(eq, use_bigram=False))
                        if k and k.strip()
                    )
                    for eq in fallback_expanded
                ]
                _prev_okms_docs = list(gr_top_docs)
                fb_docs = await collect_okms_groupa_and_gov_fallback_docs(
                    message=message,
                    reformed_query=reformed_query,
                    expanded_queries=fallback_expanded,
                    tri_built=fallback_tri,
                    per_query_limit=_GR_GA_PER_QUERY,
                    run_group_a=_group_a_run_okms_query,
                    run_gov=_run_gov_okms_query,
                    max_policy_pairs=1,
                )
                logger.info("[TIMING][guide_recommend] StepD-S2 fallback 보강검색: %.3fs", time.monotonic() - _t)
                if fb_docs:
                    gr_top_docs = sorted(
                        _deduplicate_documents(gr_top_docs + fb_docs),
                        key=lambda x: float(x.get("WEIGHT", 0) or 0),
                        reverse=True,
                    )[:_GR_FINAL_TOP_N]
                    logger.info("[RAG/guide_recommend_v2] fallback 보강 후 OKMS: %d개", len(gr_top_docs))
                    if should_rerun_sufficiency_judgment(
                        before_docs=_prev_okms_docs,
                        after_docs=gr_top_docs,
                    ) and not (excluded_chunk_ids or excluded_service_names):
                        _t = time.monotonic()
                        okms_sufficiency = await run_sufficiency_judgment_fast(
                            judge_fn=retrieval_sufficiency_judgment,
                            user_question=message,
                            intent=intent,
                            collection_name=Config.RAG_OKMS_COLLECTION,
                            docs=gr_top_docs,
                            log_prefix="RAG/guide_recommend_v2",
                        )
                        logger.info("[TIMING][guide_recommend] StepD-S3 fallback 재판단: %.3fs", time.monotonic() - _t)
                        logger.info(
                            "[RAG/guide_recommend_v2] fallback 재판단: sufficient=%s, reason=%s",
                            okms_sufficiency["sufficient"],
                            okms_sufficiency["reason"],
                        )
                    else:
                        if excluded_chunk_ids or excluded_service_names:
                            logger.info("[RAG/guide_recommend_v2] fallback 재판단 스킵: more_info 모드")
                        else:
                            logger.info("[RAG/guide_recommend_v2] fallback 재판단 스킵: 개선 폭 미미")

            if okms_sufficiency["sufficient"]:
                logger.info("[RAG/guide_recommend_v2] fallback 후 충분 — OUR_REGION_TEL 검색 생략")
            else:
                logger.info("[RAG/guide_recommend_v2] OKMS 결과 부족 → OUR_REGION_TEL 검색 진행")

                from app.chat.sigun import extract_eupmyeondong_from_message
                gr_eupmyeondong = extract_eupmyeondong_from_message(message)
                gr_eupmyeondong_filters = [gr_eupmyeondong] if gr_eupmyeondong else []

                _t = time.monotonic()
                gr_welfare_tel_docs = await run_welfare_tel_queries(
                    message, gr_sigun_filters, gr_eupmyeondong_filters,
                    _GR_WELFARE_TEL_PER_QUERY, "RAG/guide_recommend_v2",
                    excluded_chunk_ids=excluded_chunk_ids,
                    timeout_sec=(
                        float(Config.MORE_INFO_WELFARE_TEL_TIMEOUT_SEC)
                        if (excluded_chunk_ids or excluded_service_names)
                        else None
                    ),
                )
                logger.info("[TIMING][guide_recommend] StepD-W OUR_REGION_TEL 검색: %.3fs", time.monotonic() - _t)
                logger.info(f"[RAG/guide_recommend_v2] OUR_REGION_TEL 검색 합계: {len(gr_welfare_tel_docs)}개")

        # OKMS + WELFARE_TEL 합산 (OKMS 쿼터 내 정렬)
        gr_top_docs = sorted(
            _deduplicate_documents(gr_top_docs + gr_welfare_tel_docs),
            key=lambda x: float(x.get("WEIGHT", 0) or 0),
            reverse=True,
        )[:_GR_FINAL_TOP_N]

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
            message, gr_top_docs, log_prefix="[RAG/guide_recommend_v2]"
        )
        gr_top_docs = await filter_irrelevant_docs(reformed_query, gr_top_docs, sigun_filters=gr_sigun_filters)
        logger.info("[TIMING][guide_recommend] StepD-1 관련성 필터 [8b/sllm]: %.3fs", time.monotonic() - _t)
        logger.info(f"[RAG/guide_recommend_v2] 관련성 필터 후: {len(gr_top_docs)}개 문서")

        if excluded_chunk_ids or excluded_service_names:
            gr_top_docs = filter_excluded_docs(gr_top_docs, excluded_chunk_ids or [], excluded_service_names)
            logger.info(f"[MoreResults][guide_recommend] 제외 필터 후: {len(gr_top_docs)}개 문서")

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
            user_region=user_region,
            user_birth_year=str(birth_year) if birth_year else "",
            more_info_mode=bool(excluded_chunk_ids or excluded_service_names),
        )
        logger.info("[TIMING][guide_recommend] 최종 응답 생성 [32b/luxia]: %.3fs", time.monotonic() - _t)
        logger.info("[TIMING][guide_recommend] process_rag_guide_recommend 전체: %.3fs", time.monotonic() - t_total)
        return gr_response, gr_referenced_documents

    except Exception as e:
        logger.error(f"[RAG/guide_recommend_v2] 처리 중 오류: {e}", exc_info=True)
        raise
