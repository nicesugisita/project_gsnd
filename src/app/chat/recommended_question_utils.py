"""추천 후속 질문(/v1/chat/recommended-question) — 메타데이터 참조만 사용."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.core.constants import ROLE_ASSISTANT

logger = logging.getLogger(__name__)


def _synthetic_refs_from_assistant_message(msg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """메시지 루트의 referenced_service_names(+chunk_ids)로 참조 행 생성."""
    names = msg.get("referenced_service_names")
    cids = msg.get("referenced_chunk_ids")
    if not isinstance(names, list) or not names:
        return []
    out: List[Dict[str, Any]] = []
    for i, raw_name in enumerate(names):
        if not isinstance(raw_name, str) or not raw_name.strip():
            continue
        name = raw_name.strip()
        display = name[:-5] if name.lower().endswith(".hwpx") else name
        chunk_id = ""
        if isinstance(cids, list) and i < len(cids):
            chunk_id = str(cids[i] or "").strip()
        out.append({"name": display, "chunk_id": chunk_id, "snippet": "", "id": chunk_id})
    return out


def extract_metadata_referenced_documents(messages: Optional[list]) -> List[Dict[str, Any]]:
    """
    직전 assistant의 참조 목록을 수집한다.

    우선순위:
    1) ``metadata.referenced_documents`` (객체 배열)
    2) 메시지 루트 ``referenced_service_names`` (+ ``referenced_chunk_ids``)
    """
    if not messages:
        return []
    for msg in reversed(messages):
        if msg.get("role") != ROLE_ASSISTANT:
            continue
        meta = msg.get("metadata")
        if isinstance(meta, dict):
            docs = meta.get("referenced_documents")
            if isinstance(docs, list) and docs:
                filtered = [d for d in docs if isinstance(d, dict)]
                if filtered:
                    return filtered
        root_docs = msg.get("referenced_documents")
        if isinstance(root_docs, list) and root_docs:
            filtered = [d for d in root_docs if isinstance(d, dict)]
            if filtered:
                return filtered
        nested = _synthetic_refs_from_assistant_message(meta) if isinstance(meta, dict) else []
        if nested:
            return nested
        top = _synthetic_refs_from_assistant_message(msg)
        if top:
            return top
    return []


def metadata_reference_documents_to_top_docs(refs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """UI 참조 형식(name, chunk_id, snippet) → RAG top_docs 형식(NAME, CONTENT, CHUNK_ID)."""
    out: List[Dict[str, Any]] = []
    for raw in refs or []:
        name = str(raw.get("name", "") or "").strip()
        snippet = str(raw.get("snippet", "") or "").strip()
        chunk_id = str(raw.get("chunk_id", "") or raw.get("chunkId", "") or "").strip()
        doc_id = str(raw.get("id", "") or "").strip()
        if not name and not snippet:
            continue
        out.append({
            "NAME": name,
            "CONTENT": snippet,
            "CHUNK_ID": chunk_id,
            "ID": doc_id,
        })
    return out


_MAX_MARINER_QUERY_STRINGS = 8
_MARINER_PER_NAME_LIMIT = 8
_MERGED_MARINER_DOC_CAP = 12

_BUSINESS_NAME_KEYS = (
    "business_name",
    "businessName",
    "BUSINESS_NAME",
    "사업명",
)


def extract_reference_mariner_query_strings(
    refs: List[Dict[str, Any]],
    max_strings: int = _MAX_MARINER_QUERY_STRINGS,
) -> List[str]:
    """
    metadata.referenced_documents 에서 Mariner 검색어를 순서 유지·전역 중복 제거로 수집.

    항목마다 사업명 계열 키가 있으면 먼저, 이어서 문서명 ``name`` 을 넣는다.
    """
    seen: set = set()
    out: List[str] = []
    cap = max(1, max_strings)

    def _push(s: str) -> None:
        nonlocal out
        if not s or s in seen or len(out) >= cap:
            return
        seen.add(s)
        out.append(s)

    for raw in refs or []:
        if not isinstance(raw, dict):
            continue
        for key in _BUSINESS_NAME_KEYS:
            _push(str(raw.get(key, "") or "").strip())
        _push(str(raw.get("name", "") or "").strip())
        if len(out) >= cap:
            break
    return out


def fetch_mariner_docs_for_recommended_question_sync(
    meta_refs: List[Dict[str, Any]],
    sigun_filters: Optional[List[str]],
    lifecycle: Optional[str],
    year_filters: Optional[List[str]],
    excluded_chunk_ids: Optional[List[str]],
) -> List[Dict[str, Any]]:
    """
    메타의 문서명마다 GOV_OKMS 문서명 전용 Mariner 검색 후 병합.

    JVM 동기 호출 — asyncio.to_thread 등으로 이벤트 루프 밖에서 실행할 것.
    """
    from app.mariner.queryset_recommended_question import (
        query_gov_okms_documents_by_display_name,
    )
    from app.chat.infra.rag.pipeline_utils import _deduplicate_documents

    query_strings = extract_reference_mariner_query_strings(meta_refs)
    if not query_strings:
        return []

    all_docs: List[Dict[str, Any]] = []
    for q in query_strings:
        try:
            all_docs.extend(
                query_gov_okms_documents_by_display_name(
                    q,
                    year_filters=year_filters,
                    lifecycle_filter=lifecycle or None,
                    sigun_filters=sigun_filters,
                    excluded_chunk_ids=excluded_chunk_ids,
                    max_results=_MARINER_PER_NAME_LIMIT,
                )
            )
        except Exception as e:
            logger.warning("[recommended_question] GOV 검색 실패 (%s): %s", q, e)

    merged = _deduplicate_documents(all_docs)
    hint_ids = {
        str(r.get("chunk_id") or r.get("chunkId") or "").strip()
        for r in meta_refs
        if isinstance(r, dict)
    }
    hint_ids.discard("")

    def _sort_key(d: Dict[str, Any]) -> tuple:
        cid = str(d.get("CHUNK_ID", "") or "").strip()
        w = float(d.get("WEIGHT", 0) or 0)
        pref = 1.0 if cid in hint_ids else 0.0
        return (pref, w)

    merged.sort(key=_sort_key, reverse=True)
    return merged[:_MERGED_MARINER_DOC_CAP]


def ui_referenced_documents_for_response(refs: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """응답 referenced_documents 필드용 (snippet/id/path 보존)."""
    rows: List[Dict[str, str]] = []
    for raw in refs or []:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name", "") or "").strip()
        chunk_id = str(raw.get("chunk_id", "") or raw.get("chunkId", "") or "").strip()
        snippet = str(raw.get("snippet", "") or "").strip()
        doc_id = str(raw.get("id", "") or "").strip()
        path = str(raw.get("path", "") or "").strip()
        if not name and not snippet:
            continue
        rows.append({
            "name": name,
            "chunk_id": chunk_id,
            "snippet": snippet,
            "id": doc_id,
            "path": path,
        })
    return rows
