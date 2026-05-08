"""검색 쿼리 생성 유틸리티"""

import re
from typing import Dict, List

# 다른 복지 카테고리와 혼동을 일으키는 광범위 키워드
# 예) "가족지원사업" → 치매가족, 발달장애인가족 문서까지 매칭됨
_OVERLY_BROAD_KEYWORDS = {
    "가족지원사업",
    "가족지원",
}

# 하드코딩 목록 대신 어근 기반 패턴 사용:
# - "장애"가 사용자 질문에 없을 때만
# - 생성된 검색어/키워드의 "장애*" 토큰을 완화 제거
# 이렇게 하면 신규 변형어가 생겨도 목록 유지보수 없이 대응 가능.
_DISABILITY_ROOT_PATTERN = re.compile(r"장애[\w가-힣]*")


def filter_okms_keywords(keywords: List[str]) -> List[str]:
    """광범위한 키워드를 제거하여 검색 정밀도를 높임.

    한 키워드도 남지 않는 경우 원본 그대로 반환(안전 장치).
    """
    filtered = [k for k in keywords if k not in _OVERLY_BROAD_KEYWORDS]
    return filtered if filtered else keywords

def contains_disability_term(text: str) -> bool:
    """질문 텍스트에 장애 관련 키워드가 포함되어 있는지."""
    value = str(text or "")
    return bool(_DISABILITY_ROOT_PATTERN.search(value))


def sanitize_disability_keywords(keywords: List[str], user_query: str) -> List[str]:
    """질문에 장애 키워드가 없으면 장애 관련 키워드를 제거."""
    if contains_disability_term(user_query):
        return keywords
    filtered = [k for k in keywords if not _DISABILITY_ROOT_PATTERN.search(str(k or ""))]
    return filtered if filtered else keywords


def sanitize_disability_text(text: str, user_query: str) -> str:
    """질문에 장애 키워드가 없으면 검색 문장에서 장애 관련 토큰 제거."""
    value = str(text or "")
    if not value or contains_disability_term(user_query):
        return value
    value = _DISABILITY_ROOT_PATTERN.sub(" ", value)
    return re.sub(r"\s+", " ", value).strip()


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
