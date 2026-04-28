"""참조 문서 보강 및 필터링 유틸리티"""

import logging
import re
from typing import Any, Dict, List, Optional

from app.core.config import Config
from app.document.service_impl import get_document_service

logger = logging.getLogger(__name__)

INSUFFICIENT_INFO_PATTERNS = (
    "제공된 정보만으로는 해당 내용을 안내하기 어렵습니다",
)


def _enrich_referenced_documents(docs: Optional[list]) -> list:
    """문서명으로 DB에서 dataset ID를 조회해 참조 문서에 보강."""
    if not docs:
        return []
    names = [d.get("name") for d in docs if isinstance(d, dict) and d.get("name")]
    if not names:
        return []
    doc_service = get_document_service(Config)
    db_docs = doc_service.get_documents_by_names(names)
    id_by_name = {d.get("name"): d.get("id") for d in db_docs if d.get("name")}
    enriched = []
    for d in docs:
        if not isinstance(d, dict):
            continue
        name = d.get("name")
        if not name:
            continue
        enriched.append({
            "name": name,
            "id": id_by_name.get(name, d.get("id", "")) or "",
            "chunk_id": d.get("chunk_id", "") or "",
            "snippet": d.get("snippet", "") or "",
            "path": d.get("path", "") or "",
        })
    return enriched


def _normalize_for_match(text: str) -> str:
    """비교용 정규화: 공백·특수문자 제거, 소문자 변환"""
    return re.sub(r'[\s\-·•()（）]', '', text).lower()


def _is_doc_mentioned_in_response(doc_name: str, response: str, norm_response: str) -> bool:
    """문서명이 응답 텍스트에 언급되었는지 4단계 전략으로 판단."""
    if not doc_name:
        return False
    if doc_name in response:
        return True
    norm_name = _normalize_for_match(doc_name)
    if len(norm_name) >= 4 and norm_name in norm_response:
        return True
    if len(doc_name) > 10 and doc_name[:10] in response:
        return True
    # 파일명 패턴(YYYY_시군명_서비스명.확장자)에서 서비스명만 추출 후 매칭
    service_part = re.sub(r'^\d{4}_[^_]+_', '', doc_name)
    service_part = re.sub(r'\.\w+$', '', service_part)
    if service_part and service_part != doc_name and service_part in response:
        return True
    return False


def _extract_service_aliases(doc: Dict[str, Any]) -> List[str]:
    """참조 문서에서 서비스명 후보(alias)들을 추출."""
    aliases: List[str] = []
    raw_name = str(doc.get("name", "") or "").strip()
    if raw_name:
        aliases.append(raw_name)
    if raw_name:
        service_part = re.sub(r'^\d{4}_[^_]+_', '', raw_name)
        service_part = re.sub(r'\.\w+$', '', service_part).strip()
        if service_part and service_part != raw_name:
            aliases.append(service_part)
    snippet = str(doc.get("snippet", "") or "")
    for line in snippet.splitlines():
        line = line.strip()
        if not line:
            continue
        for label in ("사업명", "서비스명"):
            for prefix in (f"{label} :", f"{label}:"):
                if line.startswith(prefix):
                    value = line[len(prefix):].strip()
                    if value:
                        aliases.append(value)
    seen: set = set()
    deduped: List[str] = []
    for alias in aliases:
        key = alias.strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(key)
    return deduped


def _filter_referenced_documents_by_response(response_content: str, docs: Optional[list]) -> list:
    """최종 답변 텍스트에 언급된 참조 문서만 반환.

    매칭 결과가 0건이면 과도한 누락을 막기 위해 빈 리스트를 반환한다.
    """
    if not docs:
        return []
    response = str(response_content or "")
    norm_response = _normalize_for_match(response)
    if any(pattern in response for pattern in INSUFFICIENT_INFO_PATTERNS):
        logger.info(
            "[ReferencedDocsFilter] 정보부족 응답 감지 → 0건 반환 (입력=%d건)", len(docs)
        )
        return []
    filtered = []
    removed_names = []
    for d in docs:
        if not isinstance(d, dict):
            continue
        aliases = _extract_service_aliases(d)
        if any(_is_doc_mentioned_in_response(a, response, norm_response) for a in aliases):
            filtered.append(d)
        else:
            removed_names.append(str(d.get("name", "") or "").strip() or "(이름없음)")
    if not filtered:
        logger.info(
            "[ReferencedDocsFilter] 매칭 0건: 0건 반환 (입력=%d건)", len(docs)
        )
        return []
    logger.info(
        "[ReferencedDocsFilter] %d건 → %d건 | 제외=%s",
        len(docs), len(filtered), removed_names[:10],
    )
    return filtered
