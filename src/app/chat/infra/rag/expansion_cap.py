"""쿼리 확장 결과 상한·중복 제거 — routing 등 경량 의존 전용(순환 import 방지)."""

from __future__ import annotations

from typing import Any, List, Optional, Sequence

# DeepServer 확장 질의 개수 상한(재현성·응답시간)
MAX_EXPANDED_QUERIES_DEFAULT = 3


def dedupe_cap_expanded_queries(
    queries: Optional[Sequence[Any]],
    max_n: int = MAX_EXPANDED_QUERIES_DEFAULT,
    reformed_query: Optional[str] = None,
) -> List[str]:
    """확장 쿼리를 정규화·중복 제거 후 최대 max_n개만 유지한다.

    - reformed_query와 동일한 문자열(대소문자 무시)은 제외
    - 과도하게 긴 문자열은 제외
    """
    if not queries:
        return []
    rq = (reformed_query or "").strip().casefold()
    seen: set[str] = set()
    out: List[str] = []
    for q in queries:
        s = str(q).strip()
        if not s or len(s) > 500:
            continue
        key = s.casefold()
        if key in seen:
            continue
        if rq and key == rq:
            continue
        seen.add(key)
        out.append(s)
        if len(out) >= max_n:
            break
    return out
