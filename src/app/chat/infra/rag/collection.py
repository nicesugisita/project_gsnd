"""컬렉션 스키마 판별 유틸리티"""

from typing import Optional

from app.core.config import Config


def _uses_okms_document_schema(collection: Optional[str]) -> bool:
    """OKMS 컬렉션 전용 문서 스키마 사용 여부"""
    return (collection or "").strip().upper() == Config.RAG_OKMS_COLLECTION.upper()


def _uses_gsnd_v7_schema(collection: Optional[str]) -> bool:
    """GSND_DATASET_V8 컬렉션 스키마 사용 여부"""
    return (collection or "").strip().upper() == Config.RAG_COLLECTION.upper()


def _uses_welfare_center_schema(collection: Optional[str]) -> bool:
    """GSND_WELFARE_CENTER_V1 컬렉션 스키마 사용 여부"""
    return (collection or "").strip().upper() == Config.RAG_WELFARE_CENTER_COLLECTION.upper()
