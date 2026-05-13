"""GOV_OKMS 결과의 LIFE_CYCLE 기반 post-filter 회귀 테스트.

배경:
- GOV_OKMS WHERE 의 LIFE_CYCLE 필터(op code 34)는 OP 상수 정의에 없는 undocumented
  연산자라 동작이 일관적이지 않음.
- 예시 사례: "창원 노인 복지 추천" 질의에 lifecycle=노년 으로 검색했는데
  LIFE_CYCLE="아동, 청년, 청소년" 인 청소년복지시설 운영 지원 문서가 통과해
  상위에 노출됨.
- post-filter 는 LIFE_CYCLE 필드를 직접 파싱해 결정적으로 검증.

핵심 규칙:
- 사용자 질의에서 생애주기 추출 안 됨(None/빈값) → 필터 적용 안 함 (false negative 방지).
- 매핑된 생애주기 값(예: "노년")이 문서 LIFE_CYCLE 멀티값에 포함되면 유지.
- 문서 LIFE_CYCLE 빈값이면 전체 대상 가능성 → 유지.
"""
from __future__ import annotations

from app.chat.infra.rag.pipeline_utils import filter_gov_okms_docs_by_lifecycle


def _doc(name: str, life_cycle: str = "") -> dict:
    return {
        "NAME": name,
        "LIFE_CYCLE": life_cycle,
        "WEIGHT": "0.5",
        "CHUNK_ID": name,
    }


def test_lifecycle_none_skips_filter() -> None:
    """user_lifecycle=None: 사용자 질의에서 생애주기 추출 안 됨 → 필터 미적용."""
    docs = [
        _doc("기초연금", "노년"),
        _doc("청소년복지시설 운영 지원", "아동,청년,청소년"),
        _doc("아동수당", "아동"),
    ]
    out = filter_gov_okms_docs_by_lifecycle(docs, user_lifecycle=None)
    assert len(out) == 3, "lifecycle None 이면 원본 그대로 반환"


def test_lifecycle_empty_string_skips_filter() -> None:
    """빈 문자열도 None 과 동일 — 필터 미적용."""
    docs = [_doc("기초연금", "노년"), _doc("청소년복지시설", "청소년")]
    out = filter_gov_okms_docs_by_lifecycle(docs, user_lifecycle="")
    assert len(out) == 2


def test_elderly_query_removes_non_matching() -> None:
    """노인 질의 → 노년 매핑 → LIFE_CYCLE 에 노년 없는 문서 제거."""
    docs = [
        _doc("기초연금", "노년"),
        _doc("청소년복지시설 운영 지원", "아동,청년,청소년"),
        _doc("노인맞춤돌봄", "노년,중장년"),
        _doc("발달지원", "아동"),
    ]
    out = filter_gov_okms_docs_by_lifecycle(docs, user_lifecycle="노인")
    out_names = [d["NAME"] for d in out]
    assert "기초연금" in out_names
    assert "노인맞춤돌봄" in out_names
    assert "청소년복지시설 운영 지원" not in out_names
    assert "발달지원" not in out_names


def test_doc_with_empty_lifecycle_is_kept() -> None:
    """문서 LIFE_CYCLE 필드 빈값 → 전체 대상 가능성으로 보존 (conservative)."""
    docs = [
        _doc("기초연금", "노년"),
        _doc("전체대상 서비스", ""),    # LIFE_CYCLE 빈값
        _doc("청소년복지시설", "청소년"),
    ]
    out = filter_gov_okms_docs_by_lifecycle(docs, user_lifecycle="노인")
    out_names = [d["NAME"] for d in out]
    assert "전체대상 서비스" in out_names, "LIFE_CYCLE 빈값 문서는 보존"
    assert "청소년복지시설" not in out_names


def test_multi_value_separators() -> None:
    """LIFE_CYCLE 멀티값 구분자 다양성: 쉼표·세미콜론·슬래시·공백."""
    docs = [
        _doc("쉼표", "아동,청년,노년"),
        _doc("세미콜론", "아동;청년;노년"),
        _doc("슬래시", "아동/청년/노년"),
        _doc("공백", "아동 청년 노년"),
        _doc("미포함", "아동,청년"),
    ]
    out = filter_gov_okms_docs_by_lifecycle(docs, user_lifecycle="노인")
    out_names = [d["NAME"] for d in out]
    for n in ("쉼표", "세미콜론", "슬래시", "공백"):
        assert n in out_names, f"{n} 구분자 케이스 유지 실패"
    assert "미포함" not in out_names


def test_partial_token_no_substring_match() -> None:
    """부분 문자열 매칭 금지 — "청소년" 토큰이 "노년" 으로 매칭되면 안 됨."""
    docs = [
        _doc("청소년만", "청소년"),  # "청소년" 안에 "년" 포함되지만 토큰 단위로는 "노년" 아님
    ]
    out = filter_gov_okms_docs_by_lifecycle(docs, user_lifecycle="노인")
    assert len(out) == 0, "부분 매칭으로 통과되면 안 됨"


def test_empty_docs_input() -> None:
    assert filter_gov_okms_docs_by_lifecycle([], "노인") == []


def test_user_lifecycle_not_in_map_uses_raw() -> None:
    """매핑 dict 에 없는 lifecycle 값은 원본 그대로 비교 (예: '청소년' 직접 매칭)."""
    docs = [
        _doc("청소년1", "청소년"),
        _doc("노년1", "노년"),
    ]
    out = filter_gov_okms_docs_by_lifecycle(docs, user_lifecycle="청소년")
    out_names = [d["NAME"] for d in out]
    assert "청소년1" in out_names
    assert "노년1" not in out_names
