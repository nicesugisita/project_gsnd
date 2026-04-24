"""general 의도 RAG 처리"""

import asyncio
import logging
from typing import Any, Dict, List

import jpype

from core.config import Config
from mariner.queryset import normalize_sigun

from .document import _get_document_name, _get_document_snippet
from .extraction import (
    _birth_year_to_lifecycle,
    _extract_birth_year_from_message,
    _extract_lifecycle_from_message,
    _extract_sigun_from_message,
)
from .facility import _extract_specific_facility_name
from .pipeline_utils import _deduplicate_documents, _is_sufficient
from .query_builder import _build_search_queries
from .mariner import query_mariner_documents
from .response import _generate_final_response
from .welfare_search import (
    _search_welfare_center_documents,
    _build_welfare_referenced_documents,
)

logger = logging.getLogger(__name__)


async def _process_general_intent(
    message: str,
    reformed_query: str,
    selected_collection: str,
    temperature: float,
    max_tokens: int,
    stream: bool,
    frequency_penalty: float,
    repetition_penalty: float,
    top_p: float,
    top_k: int,
    seed: int,
    tools: list,
    status_callback,
    messages: list,
) -> tuple:
    """general 의도 처리: 일반 복지 정보 검색"""
    from services.router_service import expand_query, extract_triples

    if status_callback:
        await status_callback("최적의 답변방식을 찾고 있습니다")
    expanded_queries = await expand_query(reformed_query)
    if not expanded_queries:
        logger.warning("[RAG] 쿼리 확장 실패 - 원본 질의 사용")
        expanded_queries = [reformed_query]
    logger.info(f"[RAG] 확장 완료: {len(expanded_queries)}개 쿼리")

    if status_callback:
        await status_callback("내용을 정리하고 있습니다")
    triples_list = await asyncio.gather(
        *[extract_triples(eq) for eq in expanded_queries]
    )
    search_queries = _build_search_queries(list(triples_list))
    logger.info(f"[RAG] 키워드 추출 완료: 검색쿼리 {len(search_queries)}개")

    gen_sigun_raws = _extract_sigun_from_message(message)
    gen_birth_year = _extract_birth_year_from_message(message)
    gen_lifecycle = _birth_year_to_lifecycle(gen_birth_year) if gen_birth_year else _extract_lifecycle_from_message(message)
    _gen_normalized = [normalize_sigun(r) for r in gen_sigun_raws if r != "경남"]
    _gen_city_filters = [s for s in _gen_normalized if s.startswith("경상남도 ")]
    gen_sigun_filters = list(dict.fromkeys(_gen_city_filters + ["경상남도"])) if _gen_city_filters else ["경상남도"]
    logger.info(f"[RAG/general] OKMS 필터 - sigun: {gen_sigun_filters}, lifecycle: '{gen_lifecycle}'")

    _GEN_GA_PER_QUERY = 5
    _GEN_GA_TOP_N = 10
    if status_callback:
        await status_callback("질문을 분석하고 있습니다")

    def _run_okms_query(query, search_mode):
        try:
            if jpype.isJVMStarted() and not jpype.isThreadAttachedToJVM():
                jpype.attachThreadToJVM()
            return query_mariner_documents(
                query, Config.RAG_OKMS_COLLECTION,
                sigun_filters=gen_sigun_filters,
                lifecycle_filter=gen_lifecycle or None,
                search_mode=search_mode,
            )
        except Exception as e:
            logger.warning(f"[RAG/general] OKMS 쿼리 검색 실패: {e}")
            return []

    loop = asyncio.get_event_loop()
    ga_exp_futures = [loop.run_in_executor(None, _run_okms_query, eq, "hybrid") for eq in expanded_queries]
    ga_tri_built = []
    for keywords in triples_list:
        sq = " ".join(k.strip() for k in keywords if k and k.strip())
        ga_tri_built.append(sq)
    ga_tri_futures = []
    for sq in ga_tri_built:
        if sq:
            ga_tri_futures.append(loop.run_in_executor(None, _run_okms_query, sq, "hybrid"))
        else:
            fut = loop.create_future()
            fut.set_result([])
            ga_tri_futures.append(fut)
    ga_exp_results, ga_tri_results = await asyncio.gather(
        asyncio.gather(*ga_exp_futures),
        asyncio.gather(*ga_tri_futures),
    )

    okms_group_a_docs: List[Dict[str, Any]] = []
    for i, docs in enumerate(ga_exp_results, 1):
        if docs:
            okms_group_a_docs.extend(docs[:_GEN_GA_PER_QUERY])
            logger.info(f"[RAG/general] [GroupA] OKMS 확장쿼리 #{i}: {min(len(docs), _GEN_GA_PER_QUERY)}개 문서")
        else:
            logger.info(f"[RAG/general] [GroupA] OKMS 확장쿼리 #{i}: 0개 문서")
    for i, docs in enumerate(ga_tri_results, 1):
        if not ga_tri_built[i - 1]:
            logger.info(f"[RAG/general] [GroupA] OKMS 트리플쿼리 #{i}: 키워드 없음 - 건너뜀")
            continue
        if docs:
            okms_group_a_docs.extend(docs[:_GEN_GA_PER_QUERY])
            logger.info(f"[RAG/general] [GroupA] OKMS 트리플쿼리 #{i}: {min(len(docs), _GEN_GA_PER_QUERY)}개 문서")
        else:
            logger.info(f"[RAG/general] [GroupA] OKMS 트리플쿼리 #{i}: 0개 문서")

    okms_group_a_top = sorted(
        _deduplicate_documents(okms_group_a_docs),
        key=lambda x: float(x.get("WEIGHT", 0) or 0),
        reverse=True,
    )[:_GEN_GA_TOP_N]
    logger.info(f"[RAG/general] [GroupA] OKMS 최종: {len(okms_group_a_top)}개")

    _GEN_GB_PER_QUERY = 3
    _GEN_GB_TOP_N = 10
    gb_exp_futures = [loop.run_in_executor(None, _run_okms_query, eq, "vector") for eq in expanded_queries]
    gb_tri_futures = []
    for sq in ga_tri_built:
        if sq:
            gb_tri_futures.append(loop.run_in_executor(None, _run_okms_query, sq, "keyword"))
        else:
            fut = loop.create_future()
            fut.set_result([])
            gb_tri_futures.append(fut)
    gb_exp_results, gb_tri_results = await asyncio.gather(
        asyncio.gather(*gb_exp_futures),
        asyncio.gather(*gb_tri_futures),
    )

    okms_group_b_docs: List[Dict[str, Any]] = []
    for i, docs in enumerate(gb_exp_results, 1):
        if docs:
            okms_group_b_docs.extend(docs[:_GEN_GB_PER_QUERY])
            logger.info(f"[RAG/general] [GroupB] OKMS 벡터 확장쿼리 #{i}: {min(len(docs), _GEN_GB_PER_QUERY)}개 문서")
        else:
            logger.info(f"[RAG/general] [GroupB] OKMS 벡터 확장쿼리 #{i}: 0개 문서")
    for i, docs in enumerate(gb_tri_results, 1):
        if not ga_tri_built[i - 1]:
            logger.info(f"[RAG/general] [GroupB] OKMS 키워드 트리플쿼리 #{i}: 키워드 없음 - 건너뜀")
            continue
        if docs:
            okms_group_b_docs.extend(docs[:_GEN_GB_PER_QUERY])
            logger.info(f"[RAG/general] [GroupB] OKMS 키워드 트리플쿼리 #{i}: {min(len(docs), _GEN_GB_PER_QUERY)}개 문서")
        else:
            logger.info(f"[RAG/general] [GroupB] OKMS 키워드 트리플쿼리 #{i}: 0개 문서")

    okms_group_b_top = sorted(
        _deduplicate_documents(okms_group_b_docs),
        key=lambda x: float(x.get("WEIGHT", 0) or 0),
        reverse=True,
    )[:_GEN_GB_TOP_N]
    logger.info(f"[RAG/general] [GroupB] OKMS 최종: {len(okms_group_b_top)}개")

    _GEN_FINAL_TOP_N = 5
    okms_final = sorted(
        _deduplicate_documents(okms_group_a_top + okms_group_b_top),
        key=lambda x: float(x.get("WEIGHT", 0) or 0),
        reverse=True,
    )[:_GEN_FINAL_TOP_N]
    logger.info(f"[RAG/general] OKMS 최종: {len(okms_final)}개")

    _SUFFICIENCY_WEIGHT = 0.7
    _SUFFICIENCY_MIN = 5

    top_docs: List[Dict[str, Any]] = okms_final
    welfare_docs_gen: List[Dict[str, Any]] = []
    gsnd_top: List[Dict[str, Any]] = []

    _specific_facility = _extract_specific_facility_name(message)
    if _is_sufficient(top_docs, _SUFFICIENCY_MIN, _SUFFICIENCY_WEIGHT) and not _specific_facility:
        logger.info("[RAG/general] OKMS 결과 충분 — 복지시설/GSND 검색 생략")
    else:
        if _specific_facility:
            logger.info(f"[RAG/general] 시설명 직접 검색 감지 '{_specific_facility}' → 복지시설 검색 실행")
        else:
            logger.info("[RAG/general] OKMS 결과 부족 → 복지시설(4) 검색")
        welfare_docs_gen = _search_welfare_center_documents(
            reformed_query, message, gen_sigun_filters, lifecycle=gen_lifecycle
        )
        if welfare_docs_gen:
            logger.info(f"[RAG/general] 복지시설 검색 결과: {len(welfare_docs_gen)}개")

        combined = _deduplicate_documents(top_docs + welfare_docs_gen)
        if _is_sufficient(combined, _SUFFICIENCY_MIN, _SUFFICIENCY_WEIGHT):
            logger.info("[RAG/general] OKMS+복지시설 결과 충분 — GSND 검색 생략")
        else:
            logger.info("[RAG/general] OKMS+복지시설 결과 부족 → GSND(3) 병렬 검색")
            _GSND_SUPPLEMENT_COUNT = 2
            _GSND_PER_QUERY = 10
            gsnd_all_queries = list(expanded_queries) + list(search_queries)

            def _run_gsnd_query(query):
                try:
                    if jpype.isJVMStarted() and not jpype.isThreadAttachedToJVM():
                        jpype.attachThreadToJVM()
                    docs = query_mariner_documents(query, selected_collection, sigun_filters=gen_sigun_filters)
                    if docs:
                        return sorted(docs, key=lambda x: float(x.get("WEIGHT", 0) or 0), reverse=True)[:_GSND_PER_QUERY]
                except Exception as e:
                    logger.warning(f"[RAG/general] GSND 쿼리 검색 실패: {e}")
                return []

            gsnd_futures = [
                loop.run_in_executor(None, _run_gsnd_query, q)
                for q in gsnd_all_queries
            ]
            gsnd_results = await asyncio.gather(*gsnd_futures)

            gsnd_docs: List[Dict[str, Any]] = []
            for i, (q, result) in enumerate(zip(gsnd_all_queries, gsnd_results), 1):
                if result:
                    gsnd_docs.extend(result)
                    logger.info(f"[RAG/general] GSND 병렬쿼리 #{i}: {len(result)}개 문서")

            gsnd_top = sorted(
                _deduplicate_documents(gsnd_docs),
                key=lambda x: float(x.get("WEIGHT", 0) or 0),
                reverse=True,
            )[:_GSND_SUPPLEMENT_COUNT]
            logger.info(f"[RAG/general] GSND 검색 합계: {len(gsnd_docs)}개 → 상위 {len(gsnd_top)}개 보강")
            top_docs = sorted(
                _deduplicate_documents(okms_final + gsnd_top),
                key=lambda x: float(x.get("WEIGHT", 0) or 0),
                reverse=True,
            )

    logger.info(f"[RAG] 최종 선택: {len(top_docs)}개 문서")
    for i, doc in enumerate(top_docs, 1):
        logger.info(f"[RAG] #{i} NAME={_get_document_name(doc) or '?'}, WEIGHT={doc.get('WEIGHT', '?')}")

    seen_chunk_ids: set = set()
    referenced_documents = []
    for doc in top_docs:
        chunk_id = doc.get("CHUNK_ID", "")
        if chunk_id and chunk_id in seen_chunk_ids:
            logger.debug(f"[RAG] 중복 문서 제외: CHUNK_ID={chunk_id}")
            continue
        if chunk_id:
            seen_chunk_ids.add(chunk_id)
        referenced_documents.append({
            "id": str(doc.get("ID", "")),
            "chunk_id": str(chunk_id or ""),
            "name": _get_document_name(doc),
            "snippet": _get_document_snippet(doc),
        })
    welfare_referenced_gen = _build_welfare_referenced_documents(welfare_docs_gen)
    referenced_documents.extend(welfare_referenced_gen)
    logger.info(f"[RAG] UI 표시용 문서: {len(referenced_documents)}개")

    response = await _generate_final_response(
        message, top_docs, temperature, max_tokens, stream,
        frequency_penalty, repetition_penalty, top_p, top_k, seed, tools,
        intent="general",
        welfare_docs=welfare_docs_gen if welfare_docs_gen else None,
        lifecycle=gen_lifecycle,
        messages=messages,
    )

    return response, referenced_documents[:Config.RAG_UI_MAX_DOCS]
