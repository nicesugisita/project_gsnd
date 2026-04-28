"""
복지시설 검색 v2 — _search_welfare_center_documents의 v2 래퍼

v1(services/rag_service.py)의 _search_welfare_center_documents와 동일한 로직이며,
queryset만 mariner/queryset_welfare.py로 교체합니다.

기존 services/rag_service.py는 변경하지 않습니다.
"""

import logging
from typing import Dict, Any, List

from core.config import Config

# v2 queryset
from mariner.queryset_welfare import query_welfare_center_documents

# v1에서 재사용하는 유틸 함수/상수
from services.rag_service import (
    _extract_specific_facility_name,
    _lookup_facility_by_name,
    _extract_facility_type_from_message,
    _LIFECYCLE_EXCLUDED_FACILITY_TYPES,
)

logger = logging.getLogger(__name__)


def search_welfare_center_documents_v2(
    keyword: str,
    message: str,
    sigun_filters: List[str],
    lifecycle: str = "",
) -> List[Dict[str, Any]]:
    """복지시설 컬렉션 검색 v2 (SIGUN + 시설유형 필터 적용)

    v1과 동일한 로직이며, 검색식만 v2(4-field OR)로 교체되었습니다.

    Args:
        keyword: 벡터 검색 키워드
        message: 원본 사용자 메시지 (시설 키워드 추출에 사용)
        sigun_filters: SIGUN 필드 필터 목록
        lifecycle: 생애주기 (예: "아동") - 부적합 시설유형 제외에 사용

    Returns:
        검색된 복지시설 문서 목록 (상위 Config.RAG_NUM_REFERENCED_DOCS개)
    """
    # 시설명 직접 조회: 메시지에 구체적 시설명이 있으면 시군 없이도 바로 조회
    specific_facility_name = _extract_specific_facility_name(message)
    if specific_facility_name:
        return _lookup_facility_by_name(specific_facility_name)

    facility_type = _extract_facility_type_from_message(message)
    logger.info(f"[Welfare/v2] 시설유형 필터: '{facility_type}', SIGUN 필터: {sigun_filters}, lifecycle: '{lifecycle}'")
    try:
        docs = query_welfare_center_documents(
            keyword,
            sigun_filters=sigun_filters,
            facility_type_filter=facility_type,
        )

        # 생애주기에 맞지 않는 시설유형 제외
        excluded_types = _LIFECYCLE_EXCLUDED_FACILITY_TYPES.get(lifecycle, [])
        if excluded_types:
            before = len(docs)
            docs = [
                d for d in docs
                if str(d.get("FACILITY_TYPE", "") or "").strip() not in excluded_types
            ]
            if before != len(docs):
                logger.info(f"[Welfare/v2] lifecycle='{lifecycle}' 부적합 시설 제외: {before}개 → {len(docs)}개")

        logger.info(f"[Welfare/v2] 검색 결과: {len(docs)}개")
        return docs[:Config.RAG_NUM_REFERENCED_DOCS]
    except Exception as e:
        logger.warning(f"[Welfare/v2] 검색 실패: {e}")
        return []
