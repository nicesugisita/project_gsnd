"""검색 쿼리 생성 유틸리티"""

from typing import Dict, List

# 다른 복지 카테고리와 혼동을 일으키는 광범위 키워드
# 예) "가족지원사업" → 치매가족, 발달장애인가족 문서까지 매칭됨
_OVERLY_BROAD_KEYWORDS = {
    "가족지원사업",
    "가족지원",
}


def filter_okms_keywords(keywords: List[str]) -> List[str]:
    """광범위한 키워드를 제거하여 검색 정밀도를 높임.

    한 키워드도 남지 않는 경우 원본 그대로 반환(안전 장치).
    """
    filtered = [k for k in keywords if k not in _OVERLY_BROAD_KEYWORDS]
    return filtered if filtered else keywords


def _build_search_queries(keyword_groups: List[List[str]]) -> List[str]:
    """
    키워드 그룹 목록으로부터 검색 쿼리 생성
    각 그룹의 키워드를 join하여 하나의 검색 쿼리로 만듦

    Args:
        keyword_groups: 쿼리별 키워드 그룹 목록

    Returns:
        생성된 검색 쿼리 목록 (그룹당 1개)
    """
    search_queries = []
    for keywords in keyword_groups:
        combined = " ".join(k.strip() for k in keywords if k and k.strip())
        if combined:
            search_queries.append(combined)
    return search_queries


def _build_comparison_search_queries(triples: List[Dict[str, str]]) -> List[str]:
    """
    비교 트리플로부터 검색 쿼리 생성 (Predicate를 키워드로 포함하지 않음)

    - 비교대상: Subject + Object (두 비교 대상을 함께 검색)
    - 비교속성: Subject + Object (서비스명 + 속성명)
    - 지역:     건너뜀 (sigun 필터로만 사용)
    - UNK:      Subject 단독 검색
    """
    search_queries = []
    seen = set()

    for triple in triples:
        subj = triple.get("Subject", "").strip()
        pred = triple.get("Predicate", "").strip()
        obj = triple.get("Object", "").strip()

        if pred == "지역":
            continue

        if pred == "비교대상":
            parts = [p for p in [subj, obj] if p and p != "UNK"]
            query = " ".join(parts)
        elif pred == "비교속성":
            parts = [p for p in [subj, obj] if p and p != "UNK"]
            query = " ".join(parts)
        else:
            query = subj

        if query and query not in seen:
            seen.add(query)
            search_queries.append(query)

    return search_queries
