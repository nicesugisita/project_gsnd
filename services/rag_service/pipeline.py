"""메인 RAG 파이프라인 (디스패처)"""

import asyncio
import logging
from typing import Any, Dict

from core.config import Config
from core.constants import ROLE_USER

from .collection import _resolve_collection_for_intent
from .intent_comparison import _process_comparison_intent
from .intent_guide_recommend import _process_guide_recommend_intent
from .intent_general import _process_general_intent
from .mariner import query_mariner_documents
from .pipeline_utils import _deduplicate_documents
from .query_builder import _build_search_queries
from .response import _generate_final_response
from .token import truncate_messages_by_token_limit

logger = logging.getLogger(__name__)


async def process_with_rag(
    message: str,
    routing_result: Dict[str, Any],
    temperature: float = 0,
    max_tokens: int = None,
    conv_id: str = "",
    messages: list = None,
    reformed_query: str = None,
    stream: bool = False,
    frequency_penalty: float = 0,
    repetition_penalty: float = 1.0,
    top_p: float = 1.0,
    top_k: int = 1,
    seed: int = None,
    tools: list = None
) -> tuple:
    """
    라우팅 결과에 따라 RAG 또는 일반 LLM으로 응답 생성

    Args:
        message: 사용자 질문
        routing_result: qa_type_query() 결과
        temperature: 응답의 창의성
        max_tokens: 최대 생성 토큰 수
        conv_id: 대화 ID
        messages: 대화 히스토리
        reformed_query: 재구성된 질의
        stream: 스트리밍 응답 여부

    Returns:
        (생성된 응답, 참고 문서 목록)
    """
    from services.llm_service import call_llm_api

    qa_routing = routing_result.get("qa_routing", {})
    rag_scope = routing_result.get("rag_scope", {})
    is_rag_target = rag_scope.get("is_rag_target", False)

    if is_rag_target and Config.RAG_ENABLED:
        logger.info(f"[RAG Mode] 질문: {message[:50]}")
        return await _handle_rag_query(
            message, qa_routing.get("qa_type", ""), messages, reformed_query,
            temperature, max_tokens, stream,
            frequency_penalty, repetition_penalty, top_p, top_k, seed, tools
        )

    logger.info(f"[LLM Mode] 질문: {message[:50]}")
    response = await call_llm_api(
        temperature=temperature,
        max_tokens=max_tokens,
        messages=truncate_messages_by_token_limit(
            messages if messages else [{"role": ROLE_USER, "content": message}]
        ),
        frequency_penalty=frequency_penalty,
        repetition_penalty=repetition_penalty,
        top_p=top_p,
        top_k=top_k,
        seed=seed,
        tools=tools
    )
    return response, []


async def _handle_rag_query(
    message: str,
    qa_type: str,
    messages: list,
    reformed_query: str,
    temperature: float,
    max_tokens: int,
    stream: bool,
    frequency_penalty: float,
    repetition_penalty: float,
    top_p: float,
    top_k: int,
    seed: int,
    tools: list
) -> tuple:
    """RAG 쿼리 처리 헬퍼 함수"""
    from services.llm_service import call_llm_api
    from services.router_service import expand_query, extract_triples, select_collection_category

    try:
        base_collection = await select_collection_category(reformed_query)
        selected_collection = _resolve_collection_for_intent("general", base_collection)
        logger.info(f"[RAG] 선택된 컬렉션: {selected_collection}")

        expanded_queries, triples = await asyncio.gather(
            expand_query(reformed_query),
            extract_triples(reformed_query)
        )

        if not expanded_queries:
            logger.warning("[RAG] 쿼리 확장 실패 - 원본 질의 사용")
            expanded_queries = [reformed_query]

        logger.info(f"[RAG] 확장: {len(expanded_queries)}개 쿼리")
        logger.info(f"[RAG] 트리플: {len(triples)}개")

        all_docs_by_query = {}

        docs_reformed = query_mariner_documents(reformed_query, selected_collection)
        all_docs_by_query['reformed'] = docs_reformed[:5] if docs_reformed else []
        logger.info(f"[RAG] 재구성 쿼리 검색: {len(all_docs_by_query['reformed'])}개 문서")

        all_docs_expanded = []
        for i, eq in enumerate(expanded_queries):
            try:
                docs = query_mariner_documents(eq, selected_collection)
                if docs:
                    all_docs_expanded.extend(docs[:5])
                logger.debug(f"[RAG] 확장 쿼리 #{i+1} 검색: {len(docs) if docs else 0}개")
            except Exception as e:
                logger.warning(f"[RAG] 확장 쿼리 검색 실패: {e}")
        all_docs_by_query['expanded'] = all_docs_expanded
        logger.info(f"[RAG] 확장 쿼리 검색 합계: {len(all_docs_expanded)}개 문서")

        search_queries = _build_search_queries([triples])
        all_docs_triples = []
        if not search_queries:
            logger.info("[RAG] 키워드 검색 #1: 키워드 없음 - 건너뜀")
        for i, sq in enumerate(search_queries, 1):
            try:
                docs = query_mariner_documents(sq, selected_collection)
                if docs:
                    all_docs_triples.extend(docs[:5])
                    logger.info(f"[RAG] 키워드 검색 #{i}: {min(len(docs), 5)}개 문서")
                else:
                    logger.info(f"[RAG] 키워드 검색 #{i}: 0개 문서")
            except Exception as e:
                logger.warning(f"[RAG] 키워드 검색 #{i} 실패: {e}")
        all_docs_by_query['triples'] = all_docs_triples
        logger.info(f"[RAG] 키워드 검색 합계: {len(all_docs_triples)}개 문서")

        all_docs = all_docs_by_query['reformed'] + all_docs_by_query['expanded'] + all_docs_by_query['triples']
        unique_docs = _deduplicate_documents(all_docs)
        top3_docs = sorted(
            unique_docs,
            key=lambda x: float(x.get("WEIGHT", 0)) if x.get("WEIGHT") else 0,
            reverse=True
        )[:Config.RAG_NUM_REFERENCED_DOCS]

        logger.info(f"[RAG] 최종 문서: {len(top3_docs)}개 선택 (총 {len(unique_docs)}개 중)")
        for i, doc in enumerate(top3_docs, 1):
            logger.info(f"[RAG] #{i} NAME={doc.get('NAME', '?')}, WEIGHT={doc.get('WEIGHT', '?')}")

        referenced_documents = []
        for doc in top3_docs:
            referenced_documents.append({
                "id": str(doc.get("ID", "")),
                "chunk_id": str(doc.get("CHUNK_ID", "")),
                "name": doc.get("NAME", ""),
                "snippet": doc.get("CHUNK_PATH", "")
            })

        response = await _generate_final_response(
            message, top3_docs, temperature, max_tokens, stream,
            frequency_penalty, repetition_penalty, top_p, top_k, seed, tools
        )

        return response, referenced_documents[:Config.RAG_UI_MAX_DOCS]

    except Exception as e:
        logger.error(f"[RAG] 처리 중 오류: {e}", exc_info=True)
        from services.llm_service import call_llm_api
        return await call_llm_api(
            temperature=temperature,
            max_tokens=max_tokens,
            messages=truncate_messages_by_token_limit(
                messages if messages else [{"role": ROLE_USER, "content": message}]
            ),
            frequency_penalty=frequency_penalty,
            repetition_penalty=repetition_penalty,
            top_p=top_p,
            top_k=top_k,
            seed=seed,
            tools=tools
        )


async def process_rag_with_documents(
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
    sigun_filters: list = None,
) -> tuple:
    """
    RAG 문서 검색 및 최종 응답 생성 (intent별 전용 핸들러로 위임)

    Args:
        message: 사용자 질문
        reformed_query: 재구성된 질의
        intent: 사용자 질문 의도 ("general", "comparison", "guide_recommend")
        status_callback: 상태 메시지 콜백 함수 (optional)

    Returns:
        (응답, 참고 문서 목록)
    """
    from services.router_service import select_collection_category

    try:
        base_collection = await select_collection_category(reformed_query)
        selected_collection = _resolve_collection_for_intent(intent, base_collection)
        logger.info(f"[RAG] 선택된 컬렉션: {selected_collection}")

        kwargs = dict(
            message=message,
            reformed_query=reformed_query,
            selected_collection=selected_collection,
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
        )

        if intent == "comparison" and selected_collection == Config.RAG_OKMS_COLLECTION:
            return await _process_comparison_intent(**kwargs)

        if intent == "guide_recommend" and selected_collection == Config.RAG_OKMS_COLLECTION:
            return await _process_guide_recommend_intent(**kwargs, messages=messages)

        # general (기본)
        return await _process_general_intent(**kwargs, messages=messages)

    except Exception as e:
        logger.error(f"[RAG Document Processing] 오류: {e}", exc_info=True)
        raise
