"""스키마 판별 헬퍼 및 공통 상수 — SEP/GSND 검색 공용"""

from typing import Dict, Any, List, Optional

from core.config import Config


def _uses_okms_document_schema(collection: Optional[str]) -> bool:
    return (collection or "").strip().upper() == Config.RAG_OKMS_COLLECTION.upper()


def _uses_gsnd_v7_schema(collection: Optional[str]) -> bool:
    return (collection or "").strip().upper() == Config.RAG_COLLECTION.upper()


def _uses_welfare_center_schema(collection: Optional[str]) -> bool:
    return (collection or "").strip().upper() == Config.RAG_WELFARE_CENTER_COLLECTION.upper()


def _build_okms_document_name(doc: Dict[str, Any]) -> str:
    org_nm = str(doc.get("ORG_NM", "") or "").strip()
    if org_nm:
        return org_nm
    business_name = str(doc.get("BUSINESS_NAME", "") or "").strip()
    if business_name:
        return business_name
    sigun = str(doc.get("SIGUN", "") or "").strip()
    year = str(doc.get("YEAR", "") or "").strip()
    parts = []
    if sigun:
        parts.append(sigun)
    if year:
        parts.append(year)
    return " / ".join(parts) or str(doc.get("CHUNK_ID", "") or "문서")


_LIFECYCLE_CONTENT_KEYWORDS: Dict[str, List[str]] = {
    "영유아": ["영유아"],
    "아동": ["아동"],
    "청소년": ["청소년"],
    "청년": ["청년"],
    "중장년": ["중장년"],
    "노인": ["노인", "노년"],
}
