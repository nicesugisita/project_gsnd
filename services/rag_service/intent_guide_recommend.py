"""guide_recommend 의도 RAG 처리"""

import asyncio
import logging
from typing import Any, Dict, List

from core.config import Config
from mariner.queryset import normalize_sigun

from .document import _get_document_name, _get_document_snippet
from .extraction import (
    _birth_year_to_lifecycle,
    _extract_birth_year_from_message,
    _extract_lifecycle_from_message,
    _extract_sigun_from_message,
    _extract_years_from_message,
)
from .pipeline_utils import _deduplicate_documents
from .query_builder import _build_search_queries
from .mariner import query_mariner_documents
from .response import _generate_final_response

logger = logging.getLogger(__name__)


async def _process_guide_recommend_intent(
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
    """guide_recommend 의도 처리: 생애주기 기반 복지 서비스 추천"""
    from services.router_service import expand_query, extract_triples

    sigun_raws = _extract_sigun_from_message(message) or _extract_sigun_from_message(reformed_query)
    logger.info(f"[RAG/guide_recommend] 추출된 시군: {sigun_raws}")

    birth_year = _extract_birth_year_from_message(message)
    if not birth_year:
        years_in_msg = _extract_years_from_message(message)
        if years_in_msg:
            birth_year = int(years_in_msg[0])
            logger.info(f"[RAG/guide_recommend] 단독 연도를 출생연도로 처리: {birth_year}")

    if birth_year:
        lifecycle = _birth_year_to_lifecycle(birth_year)
        logger.info(f"[RAG/guide_recommend] 출생연도: {birth_year} → 생애주기: '{lifecycle}'")
    else:
        lifecycle = _extract_lifecycle_from_message(message)
        if lifecycle:
            logger.info(f"[RAG/guide_recommend] 생애주기 키워드 직접 추출: '{lifecycle}'")
        else:
            logger.info(f"[RAG/guide_recommend] 출생연도 추출 불가, 생애주기 필터 미적용")

    _gr_normalized = [normalize_sigun(r) for r in sigun_raws if r != "경남"]
    _gr_city_filters = [s for s in _gr_normalized if s.startswith("경상남도 ")]
    gr_sigun_filters = list(dict.fromkeys(_gr_city_filters + ["경상남도"])) if _gr_city_filters else ["경상남도"]
    logger.info(f"[RAG/guide_recommend] sigun 필터: {gr_sigun_filters}")

    gr_year_filters = _extract_years_from_message(message)
    if gr_year_filters:
        logger.info(f"[RAG/guide_recommend] 연도 필터: {gr_year_filters}")
    else:
        logger.info(f"[RAG/guide_recommend] 연도 필터 미적용")

    gr_expand_base = reformed_query
    logger.info(f"[RAG/guide_recommend] 쿼리 확장 기준: '{gr_expand_base}'")
    if status_callback:
        await status_callback("최적의 답변방식을 찾고 있습니다")
    gr_expanded = await expand_query(gr_expand_base)
    if not gr_expanded:
        logger.warning("[RAG/guide_recommend] 쿼리 확장 실패 - 기준 질의 사용")
        gr_expanded = [gr_expand_base]
    logger.info(f"[RAG/guide_recommend] 확장 완료: {len(gr_expanded)}개 쿼리")

    if status_callback:
        await status_callback("내용을 정리하고 있습니다")
    gr_triples_list = await asyncio.gather(
        *[extract_triples(eq) for eq in gr_expanded]
    )
    gr_search_queries = _build_search_queries(list(gr_triples_list))
    logger.info(f"[RAG/guide_recommend] 키워드 추출 완료: 검색쿼리 {len(gr_search_queries)}개")

    _GR_GA_PER_QUERY = 5
    _GR_GA_TOP_N = 10
    if status_callback:
        await status_callback("질문을 분석하고 있습니다")
    gr_group_a_docs: List[Dict[str, Any]] = []
    for i, eq in enumerate(gr_expanded, 1):
        try:
            docs = query_mariner_documents(
                eq, selected_collection,
                year_filters=gr_year_filters or None,
                sigun_filters=gr_sigun_filters,
                lifecycle_filter=lifecycle or None,
                search_mode="hybrid",
            )
            if docs:
                gr_group_a_docs.extend(docs[:_GR_GA_PER_QUERY])
                logger.info(f"[RAG/guide_recommend] [GroupA] 확장쿼리 #{i}: {min(len(docs), _GR_GA_PER_QUERY)}개 문서")
        except Exception as e:
            logger.warning(f"[RAG/guide_recommend] [GroupA] 확장쿼리 #{i} 검색 실패: {e}")
    for i, keywords in enumerate(gr_triples_list, 1):
        sq = " ".join(k.strip() for k in keywords if k and k.strip())
        if not sq:
            logger.info(f"[RAG/guide_recommend] [GroupA] 트리플쿼리 #{i}: 키워드 없음 - 건너뜀")
            continue
        try:
            docs = query_mariner_documents(
                sq, selected_collection,
                year_filters=gr_year_filters or None,
                sigun_filters=gr_sigun_filters,
                lifecycle_filter=lifecycle or None,
                search_mode="hybrid",
            )
            if docs:
                gr_group_a_docs.extend(docs[:_GR_GA_PER_QUERY])
                logger.info(f"[RAG/guide_recommend] [GroupA] 트리플쿼리 #{i}: {min(len(docs), _GR_GA_PER_QUERY)}개 문서")
            else:
                logger.info(f"[RAG/guide_recommend] [GroupA] 트리플쿼리 #{i}: 0개 문서")
        except Exception as e:
            logger.warning(f"[RAG/guide_recommend] [GroupA] 트리플쿼리 #{i} 검색 실패: {e}")
    gr_group_a_top = sorted(
        _deduplicate_documents(gr_group_a_docs),
        key=lambda x: float(x.get("WEIGHT", 0) or 0),
        reverse=True,
    )[:_GR_GA_TOP_N]
    logger.info(f"[RAG/guide_recommend] [GroupA] 최종: {len(gr_group_a_top)}개 (총 {len(gr_group_a_docs)}개 수집)")

    _GR_GB_PER_QUERY = 3
    _GR_GB_TOP_N = 10
    gr_group_b_docs: List[Dict[str, Any]] = []
    for i, eq in enumerate(gr_expanded, 1):
        try:
            docs = query_mariner_documents(
                eq, selected_collection,
                year_filters=gr_year_filters or None,
                sigun_filters=gr_sigun_filters,
                lifecycle_filter=lifecycle or None,
                search_mode="vector",
            )
            if docs:
                gr_group_b_docs.extend(docs[:_GR_GB_PER_QUERY])
                logger.debug(f"[RAG/guide_recommend] [GroupB] 벡터 확장쿼리 #{i}: {min(len(docs), _GR_GB_PER_QUERY)}개 문서")
        except Exception as e:
            logger.warning(f"[RAG/guide_recommend] [GroupB] 벡터 확장쿼리 #{i} 검색 실패: {e}")
    for i, keywords in enumerate(gr_triples_list, 1):
        sq = " ".join(k.strip() for k in keywords if k and k.strip())
        if not sq:
            logger.info(f"[RAG/guide_recommend] [GroupB] 키워드 트리플쿼리 #{i}: 키워드 없음 - 건너뜀")
            continue
        try:
            docs = query_mariner_documents(
                sq, selected_collection,
                year_filters=gr_year_filters or None,
                sigun_filters=gr_sigun_filters,
                lifecycle_filter=lifecycle or None,
                search_mode="keyword",
            )
            if docs:
                gr_group_b_docs.extend(docs[:_GR_GB_PER_QUERY])
                logger.info(f"[RAG/guide_recommend] [GroupB] 키워드 트리플쿼리 #{i}: {min(len(docs), _GR_GB_PER_QUERY)}개 문서")
            else:
                logger.info(f"[RAG/guide_recommend] [GroupB] 키워드 트리플쿼리 #{i}: 0개 문서")
        except Exception as e:
            logger.warning(f"[RAG/guide_recommend] [GroupB] 키워드 트리플쿼리 #{i} 검색 실패: {e}")
    gr_group_b_top = sorted(
        _deduplicate_documents(gr_group_b_docs),
        key=lambda x: float(x.get("WEIGHT", 0) or 0),
        reverse=True,
    )[:_GR_GB_TOP_N]
    logger.info(f"[RAG/guide_recommend] [GroupB] 최종: {len(gr_group_b_top)}개 (총 {len(gr_group_b_docs)}개 수집)")

    _GR_FINAL_TOP_N = 5
    gr_top_docs = sorted(
        _deduplicate_documents(gr_group_a_top + gr_group_b_top),
        key=lambda x: float(x.get("WEIGHT", 0) or 0),
        reverse=True,
    )[:_GR_FINAL_TOP_N]
    logger.info(f"[RAG/guide_recommend] 최종 선택: {len(gr_top_docs)}개")
    for i, doc in enumerate(gr_top_docs, 1):
        logger.info(f"[RAG/guide_recommend] #{i} NAME={_get_document_name(doc)}, WEIGHT={doc.get('WEIGHT', '?')}")

    gr_seen_chunk_ids: set = set()
    gr_referenced_documents: List[Dict[str, str]] = []
    for doc in gr_top_docs:
        chunk_id = doc.get("CHUNK_ID", "")
        if chunk_id and chunk_id in gr_seen_chunk_ids:
            continue
        if chunk_id:
            gr_seen_chunk_ids.add(chunk_id)
        gr_referenced_documents.append({
            "id": str(doc.get("ID", "")),
            "chunk_id": str(chunk_id or ""),
            "name": _get_document_name(doc),
            "snippet": _get_document_snippet(doc),
        })

    if status_callback:
        await status_callback("최종답변을 생성하고 있습니다")
    gr_response = await _generate_final_response(
        message, gr_top_docs, temperature, max_tokens, stream,
        frequency_penalty, repetition_penalty, top_p, top_k, seed, tools,
        intent="guide_recommend",
        lifecycle=lifecycle,
        messages=messages,
    )
    return gr_response, gr_referenced_documents[:Config.RAG_UI_MAX_DOCS]
