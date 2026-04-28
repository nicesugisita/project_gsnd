"""RAG 파이프라인 공통 유틸리티 함수"""

import logging
from typing import Any, Dict, List

from app.core.constants import ROLE_USER

from .document import _get_document_snippet, _get_document_name

logger = logging.getLogger(__name__)


def _build_documents_text(doc_list: List[Dict[str, Any]]) -> str:
    """문서 목록을 문자열로 포맷팅"""
    documents = []
    for doc in doc_list:
        chunk_path = _get_document_snippet(doc)
        chunk_id = doc.get("CHUNK_ID", "")
        name = _get_document_name(doc)
        if chunk_path:
            documents.append(f"[{chunk_id}] {name}\n{chunk_path}")
    return "\n\n---\n\n".join(documents)


def _build_qa_messages(documents_text: str, query: str) -> List[Dict[str, str]]:
    """QA 처리용 메시지 구성"""
    return [{
        "role": ROLE_USER,
        "content": f"documents: {documents_text}\n\nuser_query: {query}"
    }]


def _create_llm_params(
    temperature: float = 0,
    max_tokens: int = None,
    stream: bool = False,
    frequency_penalty: float = 0,
    repetition_penalty: float = 1.0,
    top_p: float = 1.0,
    top_k: int = 1,
    seed: int = None,
    tools: list = None
) -> Dict[str, Any]:
    """LLM API 호출용 파라미터 객체 생성"""
    params = {
        "temperature": temperature,
        "stream": stream,
        "frequency_penalty": frequency_penalty,
        "repetition_penalty": repetition_penalty,
        "top_p": top_p,
        "top_k": top_k,
    }
    if max_tokens:
        params["max_tokens"] = max_tokens
    if seed is not None:
        params["seed"] = seed
    if tools is not None:
        params["tools"] = tools
    return params


def _is_sufficient(
    docs: List[Dict[str, Any]],
    min_count: int = 5,
    weight_threshold: float = 0.75,
) -> bool:
    """WEIGHT >= weight_threshold 인 문서가 min_count개 이상이면 충분하다고 판단"""
    qualified = [d for d in docs if float(d.get("WEIGHT", 0) or 0) >= weight_threshold]
    return len(qualified) >= min_count


def _deduplicate_documents(doc_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """문서 중복 제거 (CHUNK_ID 기준, 동일 CHUNK_ID 중 WEIGHT 최고값 유지)"""
    best: Dict[str, Dict[str, Any]] = {}
    no_id_docs = []
    for doc in doc_list:
        chunk_id = doc.get("CHUNK_ID")
        if not chunk_id:
            no_id_docs.append(doc)
            continue
        weight = float(doc.get("WEIGHT", 0) or 0)
        if chunk_id not in best or weight > float(best[chunk_id].get("WEIGHT", 0) or 0):
            best[chunk_id] = doc

    unique_docs = list(best.values()) + no_id_docs
    logger.info(f"[RAG] 중복 제거: {len(doc_list)} → {len(unique_docs)}개")
    return unique_docs


