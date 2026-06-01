"""
추천 후속 질문 전용 파이프라인 (/v1/chat/recommended-question).

프론트 ``metadata.referenced_documents``의 사업명·문서명으로 Mariner(GOV_OKMS) 검색 후,
``retrieved_documents`` 본문과 함께 ``classification_llm_recommended_prompt`` 로 최종 LLM 답변을 생성한다.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from app.chat.infra.rag import (
    _extract_sigun_from_message,
    _extract_birth_year_from_message,
    _birth_year_to_lifecycle,
    _extract_lifecycle_from_message,
)
from app.shared.utils.year_filter import extract_year_filters
from app.chat.infra.rag.common import build_referenced_documents, filter_excluded_docs
from app.chat.infra.rag.response_generator import generate_final_response_v2
from app.mariner.sigun_utils import normalize_sigun
from app.chat.sigun import extract_sigun_from_referenced_documents_list
from app.chat.recommended_question_utils import (
    extract_metadata_referenced_documents,
    extract_reference_mariner_query_strings,
    fetch_mariner_docs_for_recommended_question_sync,
    metadata_reference_documents_to_top_docs,
    ui_referenced_documents_for_response,
)

logger = logging.getLogger(__name__)


async def process_rag_recommended_question(
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
    more_detail: bool = False,
    recommended_question_prompt: bool = False,
    precomputed_search_target: Optional[str] = None,
    precomputed_policy_priority_tag: Optional[str] = None,
    precomputed_lifecycle_tags: Optional[List[str]] = None,
    precomputed_household_tags: Optional[List[str]] = None,
    precomputed_topic_category: Optional[List[str]] = None,   # guide_recommend 외엔 미사용(시그니처 호환)
    precomputed_topic_keyword: Optional[List[str]] = None,
    precomputed_must_not_keywords: Optional[List[str]] = None,
    service_target: Optional[str] = "official",
    out_meta: Optional[Dict[str, Any]] = None,   # guide_recommend 경로 호환 — 이 파이프라인에서는 미사용
    is_drilldown: bool = False,                   # guide_recommend 경로 호환 — 이 파이프라인에서는 미사용
) -> Tuple[Any, List[Dict[str, str]]]:
    """
    호출 시그니처는 process_rag_guide_recommend와 동일하게 유지한다.
    (라우터/스트리밍에서 동일 kwargs로 호출 가능)
    """
    _ = (
        precomputed_expanded_queries,
        precomputed_keywords,
        more_detail,
        recommended_question_prompt,
        intent,
        precomputed_search_target,
        precomputed_policy_priority_tag,
        service_target,  # recommended_question은 GSND 미사용 — 시그니처 호환 위해 받기만 함
    )

    try:
        t_total = time.monotonic()
        meta_refs = extract_metadata_referenced_documents(messages or [])
        logger.info("[RAG/recommended_question] 메타 참조 %d건", len(meta_refs))

        if sigun_filters is not None:
            sigun_raws = [s.replace("경상남도 ", "") for s in sigun_filters]
            gr_sigun_filters = list(sigun_filters)
        else:
            sigun_raws = _extract_sigun_from_message(message) or _extract_sigun_from_message(reformed_query)
            _norm = [normalize_sigun(r) for r in sigun_raws if r and r != "경남"]
            _city = [s for s in _norm if s.startswith("경상남도 ")]
            gr_sigun_filters = list(dict.fromkeys(_city)) if _city else []

        if not gr_sigun_filters and meta_refs:
            bolstered = extract_sigun_from_referenced_documents_list(meta_refs)
            if bolstered:
                gr_sigun_filters = bolstered
                sigun_raws = [s.replace("경상남도 ", "") for s in gr_sigun_filters]
                logger.info(
                    "[RAG/recommended_question] 메타 참조 문서명에서 시군 보강: %s",
                    gr_sigun_filters,
                )

        birth_year = _extract_birth_year_from_message(message)
        if birth_year:
            lifecycle = _birth_year_to_lifecycle(birth_year)
        else:
            lifecycle = _extract_lifecycle_from_message(message) or ""

        rq_year_filters = extract_year_filters(message) or None
        if rq_year_filters:
            logger.debug(
                "[RAG/recommended_question] 연도 필터: %s", rq_year_filters
            )

        gr_top_docs: List[Dict[str, Any]] = []
        if extract_reference_mariner_query_strings(meta_refs):
            if status_callback:
                await status_callback("참고 문서를 검색하고 있습니다")
            gr_top_docs = await asyncio.to_thread(
                fetch_mariner_docs_for_recommended_question_sync,
                meta_refs,
                gr_sigun_filters or None,
                None,
                rq_year_filters,
                excluded_chunk_ids,
            )
            if gr_top_docs and (excluded_chunk_ids or excluded_service_names):
                gr_top_docs = filter_excluded_docs(
                    gr_top_docs,
                    list(excluded_chunk_ids or []),
                    excluded_service_names,
                )
            logger.info(
                "[RAG/recommended_question] Mariner 검색 후 문서 %d건",
                len(gr_top_docs),
            )

        if not gr_top_docs:
            gr_top_docs = metadata_reference_documents_to_top_docs(meta_refs)
            logger.info(
                "[RAG/recommended_question] Mariner 결과 없음 — 메타 스니펫 폴백 %d건",
                len(gr_top_docs),
            )

        if status_callback:
            await status_callback("최종답변을 생성하고 있습니다")

        user_region = " ".join(r for r in sigun_raws if r != "경남") or ""
        _final_user_msg = (final_user_message or "").strip() or message

        if gr_top_docs:
            gr_referenced_documents = build_referenced_documents(gr_top_docs)
        else:
            gr_referenced_documents = ui_referenced_documents_for_response(meta_refs)

        _t = time.monotonic()
        gr_response = await generate_final_response_v2(
            _final_user_msg,
            gr_top_docs,
            temperature,
            max_tokens,
            stream,
            frequency_penalty,
            repetition_penalty,
            top_p,
            top_k,
            seed,
            tools,
            intent="guide_recommend",
            lifecycle=lifecycle or "",
            messages=messages,
            user_region=user_region,
            user_birth_year=str(birth_year) if birth_year else "",
            more_info_mode=bool(excluded_chunk_ids or excluded_service_names),
            use_llm_recommended_prompt=True,
        )
        logger.info(
            "[TIMING][recommended_question] 전체: %.3fs (최종 LLM: %.3fs)",
            time.monotonic() - t_total,
            time.monotonic() - _t,
        )
        return gr_response, gr_referenced_documents
    except Exception as e:
        logger.error("[RAG/recommended_question] 처리 실패: %s", e, exc_info=True)
        raise
