"""comparison 의도 RAG 처리"""

import logging
import re
from typing import Any, Dict, List

from core.config import Config
from mariner.queryset import normalize_sigun

from .document import _get_document_name, _get_document_snippet
from .extraction import _extract_sigun_from_message, _extract_years_from_message
from .pipeline_utils import _deduplicate_documents
from .query_builder import _build_comparison_search_queries
from .mariner import query_mariner_documents
from .response import _generate_final_response
from .welfare_search import (
    _search_welfare_center_documents,
    _build_welfare_referenced_documents,
    _rerank_comparison_docs,
)

logger = logging.getLogger(__name__)


async def _process_comparison_intent(
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
) -> tuple:
    """comparison 의도 처리: 지역 간 복지 서비스 비교"""
    from services.router_service import (
        extract_comparison_attributes,
        extract_comparison_triples,
    )

    if status_callback:
        await status_callback("내용을 정리하고 있습니다")

    import asyncio
    comp_attributes, comp_triples = await asyncio.gather(
        extract_comparison_attributes(reformed_query),
        extract_comparison_triples(reformed_query),
    )
    logger.info(f"[RAG/comparison] 속성: {comp_attributes}")
    logger.info(f"[RAG/comparison] 트리플: {comp_triples}")

    comp_sigun_filter = ["경상남도"]
    found_regions: List[str] = []
    for triple in comp_triples:
        if triple.get("Predicate") == "지역":
            sigun_raw = str(triple.get("Object", "") or "").strip()
            if not sigun_raw:
                continue
            sigun_normalized = normalize_sigun(sigun_raw)
            if sigun_normalized.startswith("경상남도 "):
                found_regions.append(sigun_normalized)
            elif sigun_normalized == "경상남도":
                pass
            else:
                key = re.sub(r'[시군]$', '', sigun_normalized)
                found_regions.append(f"경상남도 {key}시")
                found_regions.append(f"경상남도 {key}군")

    sigun_from_msgs = _extract_sigun_from_message(message)
    non_gyeongnam = [s for s in sigun_from_msgs if s != "경남"]
    for sigun_raw in non_gyeongnam:
        sigun_normalized_msg = normalize_sigun(sigun_raw)
        if sigun_normalized_msg.startswith("경상남도 "):
            if sigun_normalized_msg not in found_regions:
                found_regions.append(sigun_normalized_msg)
        else:
            key = re.sub(r'[시군]$', '', sigun_raw)
            for candidate in [f"경상남도 {key}시", f"경상남도 {key}군"]:
                if candidate not in found_regions:
                    found_regions.append(candidate)

    if found_regions:
        comp_sigun_filter = list(dict.fromkeys(found_regions + ["경상남도"]))
        logger.info(f"[RAG/comparison] sigun 필터: {comp_sigun_filter}")
    else:
        logger.info("[RAG/comparison] 지역 미추출 - 기본 필터 '경상남도' 적용")

    comp_year_filter = _extract_years_from_message(message)
    if comp_year_filter:
        logger.info(f"[RAG/comparison] 연도 필터: {comp_year_filter}")
    else:
        logger.info("[RAG/comparison] 연도 필터 미적용")

    search_triples = [t for t in comp_triples if t.get("Predicate") != "지역"]
    search_queries_comp = _build_comparison_search_queries(search_triples)
    if not search_queries_comp:
        search_queries_comp = [reformed_query]
    logger.info(f"[RAG/comparison] 검색 쿼리 {len(search_queries_comp)}개: {search_queries_comp}")

    if status_callback:
        await status_callback("내용을 정리하고 있습니다")
    all_docs_comp: List[Dict[str, Any]] = []
    for i, sq in enumerate(search_queries_comp, 1):
        try:
            docs = query_mariner_documents(
                sq, selected_collection,
                sigun_filters=comp_sigun_filter,
                year_filters=comp_year_filter,
            )
            if docs:
                all_docs_comp.extend(docs)
                logger.info(f"[RAG/comparison] 검색쿼리 #{i} '{sq[:30]}': {len(docs)}개")
            else:
                logger.info(f"[RAG/comparison] 검색쿼리 #{i} '{sq[:30]}': 결과 없음")
        except Exception as e:
            logger.warning(f"[RAG/comparison] 검색쿼리 #{i} 실패: {e}")

    unique_docs_comp = _deduplicate_documents(all_docs_comp)
    n_comp_regions = len([s for s in found_regions if s != "경상남도"])
    balance_siguns_arg = found_regions if n_comp_regions >= 2 else None
    top_docs_comp = _rerank_comparison_docs(
        unique_docs_comp, comp_attributes, top_n=5, balance_siguns=balance_siguns_arg
    )
    logger.info(f"[RAG/comparison] 최종 선택: {len(top_docs_comp)}개 (후보 {len(unique_docs_comp)}개 중)")
    for i, doc in enumerate(top_docs_comp, 1):
        logger.info(f"[RAG/comparison] #{i} NAME={_get_document_name(doc)}, WEIGHT={doc.get('WEIGHT', '?')}")

    seen_ids: set = set()
    referenced_documents_comp: List[Dict[str, str]] = []
    for doc in top_docs_comp:
        cid = doc.get("CHUNK_ID", "")
        if cid and cid in seen_ids:
            continue
        if cid:
            seen_ids.add(cid)
        referenced_documents_comp.append({
            "id": str(doc.get("ID", "")),
            "chunk_id": str(cid or ""),
            "name": _get_document_name(doc),
            "snippet": _get_document_snippet(doc),
        })

    welfare_docs_comp = _search_welfare_center_documents(
        reformed_query, message, comp_sigun_filter
    )
    welfare_referenced_comp = _build_welfare_referenced_documents(welfare_docs_comp)

    if status_callback:
        await status_callback("최종답변을 생성하고 있습니다")
    response_comp = await _generate_final_response(
        message, top_docs_comp, temperature, max_tokens, stream,
        frequency_penalty, repetition_penalty, top_p, top_k, seed, tools,
        intent="comparison",
        welfare_docs=welfare_docs_comp if welfare_docs_comp else None,
    )
    referenced_documents_comp.extend(welfare_referenced_comp)
    return response_comp, referenced_documents_comp[:Config.RAG_UI_MAX_DOCS]
