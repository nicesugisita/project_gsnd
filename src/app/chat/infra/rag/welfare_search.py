"""복지시설 Mariner/DB 검색 및 리랭킹"""

import logging
from typing import Any, Dict, List, Optional

from app.core.config import Config
from app.mariner.queryset_welfare import query_welfare_center_documents

from app.chat.infra.db.welfare import _lookup_facility_from_db
from .facility import _extract_facility_type_from_message, _extract_specific_facility_name

logger = logging.getLogger(__name__)

# 생애주기별 복지시설에서 제외할 시설유형 목록
_LIFECYCLE_EXCLUDED_FACILITY_TYPES: Dict[str, List[str]] = {
    "영유아":  ["경로당", "노인복지관", "노인요양원", "노인요양공동생활가정", "노인공동생활가정", "노인교실"],
    "아동":    ["경로당", "노인복지관", "노인요양원", "노인요양공동생활가정", "노인공동생활가정", "노인교실"],
    "청소년":  ["경로당", "노인복지관", "노인요양원", "노인요양공동생활가정", "노인공동생활가정", "노인교실"],
    "청년":    ["경로당", "노인복지관", "노인요양원", "노인요양공동생활가정", "노인공동생활가정", "노인교실"],
    "중장년":  ["경로당", "노인복지관", "노인요양원", "노인요양공동생활가정", "노인공동생활가정", "노인교실"],
}


def _lookup_facility_by_name(facility_name: str) -> List[Dict[str, Any]]:
    """FACILITY_NAME으로 복지시설 직접 조회 (시군 필터 없이)

    1) okms2.TB_OFFICE LIKE 검색 (빠르고 정확)
    2) 없으면 Mariner keyword 검색 fallback
    """
    logger.info(f"[Welfare/NameLookup] 시설명 직접 조회: '{facility_name}'")

    db_results = _lookup_facility_from_db(facility_name)
    if db_results:
        return db_results

    logger.info(f"[Welfare/NameLookup] DB 결과 없음 → Mariner fallback")
    try:
        docs = query_welfare_center_documents(facility_name)
        matched = [
            d for d in docs
            if facility_name in str(d.get("FACILITY_NAME", "") or "")
        ]
        logger.info(f"[Welfare/NameLookup] Mariner 결과: {len(docs)}개 → 이름 매칭: {len(matched)}개")
        return matched[:Config.RAG_NUM_REFERENCED_DOCS]
    except Exception as e:
        logger.warning(f"[Welfare/NameLookup] Mariner 조회 실패: {e}")
        return []


def _search_welfare_center_documents(
    keyword: str,
    message: str,
    sigun_filters: List[str],
    lifecycle: str = "",
) -> List[Dict[str, Any]]:
    """복지시설 컬렉션 검색 (SIGUN + 시설유형 필터 적용)

    Args:
        keyword: 벡터 검색 키워드
        message: 원본 사용자 메시지 (시설 키워드 추출에 사용)
        sigun_filters: SIGUN 필드 필터 목록
        lifecycle: 생애주기 - 부적합 시설유형 제외에 사용

    Returns:
        검색된 복지시설 문서 목록
    """
    specific_facility_name = _extract_specific_facility_name(message)
    if specific_facility_name:
        return _lookup_facility_by_name(specific_facility_name)

    facility_type = _extract_facility_type_from_message(message)
    logger.info(f"[Welfare] 시설유형 필터: '{facility_type}', SIGUN 필터: {sigun_filters}, lifecycle: '{lifecycle}'")
    try:
        docs = query_welfare_center_documents(
            keyword,
            sigun_filters=sigun_filters,
            facility_type_filter=facility_type,
        )

        excluded_types = _LIFECYCLE_EXCLUDED_FACILITY_TYPES.get(lifecycle, [])
        if excluded_types:
            before = len(docs)
            docs = [
                d for d in docs
                if str(d.get("FACILITY_TYPE", "") or "").strip() not in excluded_types
            ]
            if before != len(docs):
                logger.info(f"[Welfare] lifecycle='{lifecycle}' 부적합 시설 제외: {before}개 → {len(docs)}개")

        logger.info(f"[Welfare] 검색 결과: {len(docs)}개")
        return docs[:Config.RAG_NUM_REFERENCED_DOCS]
    except Exception as e:
        logger.warning(f"[Welfare] 검색 실패: {e}")
        return []


def _build_welfare_referenced_documents(welfare_docs: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """복지시설 문서 목록을 UI 표시용 referenced_documents 형식으로 변환"""
    result = []
    seen = set()
    for doc in welfare_docs:
        cid = doc.get("CHUNK_ID", "")
        if cid and cid in seen:
            continue
        if cid:
            seen.add(cid)

        snippet_parts = []
        for field, label in [
            ("FACILITY_TYPE",     "시설유형"),
            ("FACILITY_CATEGORY", "분류"),
            ("ADDRESS",           "주소"),
            ("TEL",               "전화"),
            ("HOMEPAGE",          "홈페이지"),
        ]:
            val = str(doc.get(field, "") or "").strip()
            if val:
                snippet_parts.append(f"{label}: {val}")
        snippet = "\n".join(snippet_parts)

        result.append({
            "id": str(doc.get("ID", "")),
            "chunk_id": str(cid or ""),
            "name": str(doc.get("FACILITY_NAME", "") or "").strip(),
            "snippet": snippet,
            "source": "welfare_center",
        })
    return result


def _rerank_comparison_docs(
    docs: List[Dict[str, Any]],
    attributes: List[str],
    top_n: int = 5,
    balance_siguns: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    비교 검색 결과 리랭킹

    Args:
        docs: 중복 제거된 문서 목록
        attributes: 비교 속성 목록
        top_n: 최종 반환 문서 수
        balance_siguns: 지역 균형 선택 시 사용할 시군 목록

    Returns:
        리랭킹된 문서 목록 (점수 내림차순, top_n개)
    """
    def score(doc: Dict[str, Any]) -> float:
        return float(doc.get("WEIGHT", 0) or 0)

    scored = sorted(docs, key=score, reverse=True)

    region_list = [s for s in (balance_siguns or []) if s != "경상남도"]
    if len(region_list) >= 2:
        per_region = max(2, top_n // len(region_list))
        counts: Dict[str, int] = {s: 0 for s in region_list}
        result: List[Dict[str, Any]] = []
        remaining: List[Dict[str, Any]] = []
        for doc in scored:
            doc_sigun = str(doc.get("SIGUN", "") or "").strip()
            placed = False
            for sigun in region_list:
                if sigun == doc_sigun and counts[sigun] < per_region:
                    result.append(doc)
                    counts[sigun] += 1
                    placed = True
                    break
            if not placed:
                remaining.append(doc)
        slots_left = top_n - len(result)
        result.extend(remaining[:slots_left])
        return sorted(result[:top_n], key=score, reverse=True)

    return scored[:top_n]
