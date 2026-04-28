"""
RAG 단계별 컬렉션 공통 유틸
"""

from typing import Any, Dict, List

from services.rag_service import (
    _build_welfare_referenced_documents,
    _get_document_name,
    _get_document_snippet,
)


def is_welfare_stage_docs(docs: List[Dict[str, Any]]) -> bool:
    if not docs:
        return False
    sample = docs[0]
    return any(
        str(sample.get(key, "") or "").strip()
        for key in ("FACILITY_NAME", "FACILITY_TYPE", "ADDRESS")
    )


def build_stage_referenced_documents(
    docs: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    if is_welfare_stage_docs(docs):
        return _build_welfare_referenced_documents(docs)

    seen_chunk_ids: set[str] = set()
    referenced_documents: List[Dict[str, str]] = []
    for doc in docs:
        chunk_id = doc.get("CHUNK_ID", "")
        if chunk_id and chunk_id in seen_chunk_ids:
            continue
        if chunk_id:
            seen_chunk_ids.add(chunk_id)
        referenced_documents.append({
            "id": str(doc.get("ID", "")),
            "chunk_id": str(chunk_id or ""),
            "name": _get_document_name(doc),
            "snippet": _get_document_snippet(doc),
        })
    return referenced_documents
