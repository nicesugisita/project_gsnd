"""resolve_reused_intent_on_more 회귀 테스트.

배경(2026-05 운영 사례):
- 사용자 흐름:
  Turn 1 : "장애아동수당에 대해 알려줘" (clarify로 시군 되묻기)
  Turn 2 : "함안" → intent=general (장애아동수당 답변)
  Turn 3 : "가야읍 행정복지센터 연락처 알려줘" → intent=search
  Turn 4 : "장애아동수당에 대해 좀 더 자세히 알려줘" → NextIntent=MORE_DETAIL
- Turn 4 가 의도는 Turn 2 의 사업(장애아동수당) 디테일 요청이지만,
  과거 로직은 prior_intent(=search, Turn 3)만 보고 무조건 search 유지 → 시설 검색 실패.
- 수정 후: search 는 "시설+연락처/주소" 류 정의이므로, 현재 발화에 그런 키워드가
  없으면 사업 디테일로 보고 general 로 좁힘.
"""
from __future__ import annotations

import pytest

from app.chat.intent_registry import (
    resolve_reused_intent_on_more,
    _is_facility_contact_query,
)


# ---------------------------------------------------------------------------
# _is_facility_contact_query — 연락처/위치 keyword 감지
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text, expected",
    [
        # 연락처/위치 keyword 포함 → True
        ("행정복지센터 연락처 알려줘", True),
        ("가야읍 전화번호 다시", True),
        ("주소 어디야", True),
        ("위치 알려줘", True),
        ("홈페이지 주소", True),
        ("길찾기 부탁해", True),
        # 사업/제도 디테일 요청 (연락처 keyword 없음) → False
        ("장애아동수당에 대해 좀 더 자세히 알려줘", False),
        ("기초연금 조건이 뭐야", False),
        ("신청 방법 알려줘", False),
        ("운영시간 알려줘", False),
        ("어떤 서비스가 있어", False),
        # 빈/공백 → False
        ("", False),
        ("   ", False),
    ],
)
def test_is_facility_contact_query(text: str, expected: bool) -> None:
    assert _is_facility_contact_query(text) is expected


# ---------------------------------------------------------------------------
# resolve_reused_intent_on_more — MORE_INFO/MORE_DETAIL 라우팅
# ---------------------------------------------------------------------------

def test_more_info_always_returns_guide_recommend() -> None:
    """MORE_INFO 는 prior_intent 무관하게 guide_recommend (기존 디자인 유지)."""
    for prior in ("general", "comparison", "guide_recommend", "search", None, "unknown"):
        assert resolve_reused_intent_on_more(prior, more_detail=False) == "guide_recommend"


def test_more_detail_search_with_contact_keyword_keeps_search() -> None:
    """MORE_DETAIL + prior=search + 연락처 keyword 있음 → search 유지."""
    out = resolve_reused_intent_on_more(
        "search",
        more_detail=True,
        user_message="가야읍 행정복지센터 전화번호 다시 알려줘",
    )
    assert out == "search"


def test_more_detail_search_without_contact_keyword_falls_back_to_general() -> None:
    """MORE_DETAIL + prior=search + 연락처 keyword 없음 → general (regression 핵심)."""
    out = resolve_reused_intent_on_more(
        "search",
        more_detail=True,
        user_message="장애아동수당에 대해 좀 더 자세히 알려줘",
    )
    assert out == "general"


def test_more_detail_search_facility_operation_hours_falls_back_to_general() -> None:
    """시설 이름이 있어도 운영시간/서비스 내용은 search 가 아니라 general."""
    out = resolve_reused_intent_on_more(
        "search",
        more_detail=True,
        user_message="가야읍 행정복지센터 운영시간 알려줘",
    )
    # 사용자 정책: search 는 연락처/주소 검색 전용. 운영시간 등은 general.
    assert out == "general"


def test_more_detail_general_returns_general() -> None:
    """prior=general 이면 그대로 general."""
    out = resolve_reused_intent_on_more(
        "general",
        more_detail=True,
        user_message="더 자세히 알려줘",
    )
    assert out == "general"


def test_more_detail_guide_recommend_returns_general() -> None:
    """prior=guide_recommend 이면 디테일 요청은 general 로 좁힘 (기존 디자인 유지)."""
    out = resolve_reused_intent_on_more(
        "guide_recommend",
        more_detail=True,
        user_message="신청 방법 자세히",
    )
    assert out == "general"


def test_more_detail_with_no_user_message_defaults_to_general() -> None:
    """user_message 가 비어 있으면 search 로 못 가고 general 로 폴백."""
    out = resolve_reused_intent_on_more(
        "search",
        more_detail=True,
        user_message="",
    )
    assert out == "general"


def test_more_detail_backward_compat_no_user_message_keyword_arg() -> None:
    """user_message 가 키워드 인자로 빠져 있어도 호출 가능 (default='')."""
    out = resolve_reused_intent_on_more("search", more_detail=True)
    # 비어 있으므로 연락처 keyword 매칭 안 됨 → general
    assert out == "general"
