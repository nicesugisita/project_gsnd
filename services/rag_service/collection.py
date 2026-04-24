"""의도(intent)별 컬렉션 결정 유틸리티"""

from typing import Optional

from core.config import Config

INTENT_COLLECTION_MAP = {
    "comparison": Config.RAG_OKMS_COLLECTION,
    "guide_recommend": Config.RAG_OKMS_COLLECTION,
}

_INTENT_ALIAS_MAP = {
    "guide-recommend": "guide_recommend",
    "guide recommend": "guide_recommend",
}


def _normalize_intent(intent: Optional[str]) -> str:
    normalized = (intent or "general").strip().lower()
    return _INTENT_ALIAS_MAP.get(normalized, normalized)


def _resolve_collection_for_intent(intent: str, fallback_collection: Optional[str] = None) -> str:
    """의도별 RAG 컬렉션 결정"""
    normalized_intent = _normalize_intent(intent)
    if normalized_intent in INTENT_COLLECTION_MAP:
        return INTENT_COLLECTION_MAP[normalized_intent]
    return fallback_collection or Config.RAG_COLLECTION


def _uses_okms_document_schema(collection: Optional[str]) -> bool:
    """OKMS 컬렉션 전용 문서 스키마 사용 여부"""
    return (collection or "").strip().upper() == Config.RAG_OKMS_COLLECTION.upper()


def _uses_gsnd_v7_schema(collection: Optional[str]) -> bool:
    """GSND_DATASET_V8 컬렉션 스키마 사용 여부"""
    return (collection or "").strip().upper() == Config.RAG_COLLECTION.upper()


def _uses_welfare_center_schema(collection: Optional[str]) -> bool:
    """GSND_WELFARE_CENTER_V1 컬렉션 스키마 사용 여부"""
    return (collection or "").strip().upper() == Config.RAG_WELFARE_CENTER_COLLECTION.upper()
