"""
services_v2 공통 유틸리티

rag_general, rag_guide_recommend, rag_search에서 공유하는 함수들을 모아둡니다.
"""

import logging
import math
import re
import asyncio
from typing import Dict, Any, List, Optional

from mariner_v2.queryset_welfare_tel import query_welfare_tel_documents
from services.rag_service import _get_document_name, _get_document_snippet

logger = logging.getLogger(__name__)


# ============================================================
# 정렬 유틸
# ============================================================

def sort_key_year_weight(doc: Dict[str, Any]) -> tuple:
    """정렬 키: YEAR 최신순 → 동일 연도 시 WEIGHT 높은 순.

    OKMS 문서는 YEAR 필드, GSND 문서는 COMPLI_DT 필드에서 4자리 연도를 추출합니다.
    """
    year_str = str(doc.get("YEAR", "") or "").strip()
    if not year_str:
        year_str = str(doc.get("COMPLI_DT", "") or "").strip()
    year_match = re.search(r'(\d{4})', year_str)
    year_val = int(year_match.group(1)) if year_match else 0
    weight_val = float(doc.get("WEIGHT", 0) or 0)
    return (year_val, weight_val)


def sort_weight_top30_then_year(docs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """웨이트 상위 30%는 최신순(YEAR 내림차순)으로 정렬하고,
    나머지 70%는 웨이트 내림차순으로 아래에 붙인다.

    - 상위 그룹 기준: 전체 문서를 WEIGHT 내림차순 정렬 후 상위 ceil(30%) 개
    - 상위 그룹 정렬: YEAR 내림차순 → 같은 연도면 WEIGHT 내림차순
    - 하위 그룹 정렬: WEIGHT 내림차순 유지
    """
    if not docs:
        return docs

    sorted_by_weight = sorted(docs, key=lambda x: float(x.get("WEIGHT", 0) or 0), reverse=True)
    cutoff = max(1, math.ceil(len(sorted_by_weight) * 0.3))
    top_group = sorted_by_weight[:cutoff]
    bottom_group = sorted_by_weight[cutoff:]

    top_sorted = sorted(top_group, key=sort_key_year_weight, reverse=True)
    return top_sorted + bottom_group


# ============================================================
# 참고 문서 구성
# ============================================================

def build_referenced_documents(docs: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """CHUNK_ID 기준 중복 제거 후 참고 문서 목록 생성."""
    seen_chunk_ids: set = set()
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
            "path": str(doc.get("PATH", "") or doc.get("CHUNK_PATH", "") or ""),
        })
    return referenced_documents


def filter_excluded_docs(
    docs: List[Dict[str, Any]],
    excluded_chunk_ids: List[str],
    excluded_service_names: List[str] = None,
) -> List[Dict[str, Any]]:
    """이미 사용자에게 보여준 문서를 제외.

    chunk_id와 서비스명(NAME) 모두 비교하여 같은 서비스가 다른 청크로
    재등장하는 중복을 방지합니다.
    """
    if not excluded_chunk_ids and not excluded_service_names:
        return docs
    excluded_chunk_set = set(excluded_chunk_ids or [])
    excluded_name_set = set(n for n in (excluded_service_names or []) if n)
    result = []
    for d in docs:
        chunk_id = str(d.get("CHUNK_ID", ""))
        if chunk_id and chunk_id in excluded_chunk_set:
            continue
        if excluded_name_set:
            doc_name = str(d.get("NAME", "") or d.get("BUSINESS_NAME", "") or "").strip()
            if doc_name and doc_name in excluded_name_set:
                continue
        result.append(d)
    return result


# ============================================================
# OUR_REGION_TEL 검색 유틸
# ============================================================

async def run_welfare_tel_queries(
    keyword: str,
    sigun_filters: List[str],
    eupmyeondong_filters: Optional[List[str]],
    per_query: int,
    log_prefix: str,
) -> List[Dict[str, Any]]:
    """사용자 키워드로 GSND_OUR_REGION_TEL 검색을 수행한다.

    Args:
        keyword: 검색 키워드 (사용자 원래 질문)
        sigun_filters: SIGUN 필터 목록 (예: ["경상남도 창원시"])
        eupmyeondong_filters: 읍면동 필터 목록 (예: ["동읍"])
        per_query: 최대 수집 건수
        log_prefix: 로그 식별자 (e.g. "RAG/general_v2")

    Returns:
        수집된 OUR_REGION_TEL 문서 목록
    """
    if not keyword:
        return []

    loop = asyncio.get_event_loop()

    def _run():
        try:
            return query_welfare_tel_documents(
                keyword,
                sigun_filters=sigun_filters,
                eupmyeondong_filters=eupmyeondong_filters,
            )
        except Exception as e:
            logger.warning(f"[{log_prefix}] OUR_REGION_TEL 검색 실패: {e}")
            return []

    docs = await loop.run_in_executor(None, _run)
    result_docs = docs[:per_query] if docs else []
    logger.info(f"[{log_prefix}] OUR_REGION_TEL '{keyword[:30]}': {len(result_docs)}개")
    return result_docs
