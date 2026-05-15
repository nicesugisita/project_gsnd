"""Mariner 사업명 앵커 추출(_extract_business_anchors) 회귀 테스트.

배경(2026-05 운영 사례):
- 사용자 질의: "장애수당이랑 장애아동수당은 달라요?"
- reformed_query: "합천 장애수당과 장애아동수당 차이"
- 기존 `_extract_business_anchor` (단수) 는 길이 1개만 반환 →
  substring 관계인 "장애수당" 이 "장애아동수당" 에 흡수되어 한 쪽만 검색에 boost
- 결과: 비교 답변 불가 ("장애수당" 사업 자료가 검색 결과에서 누락)

수정: `_extract_business_anchors` (복수) 가 substring 관계 사업명을 모두 후보로 반환.
호출부는 OR 로 묶어 양쪽 사업 모두 검색에 boost.
"""
from __future__ import annotations

from app.mariner.queryset_okms import (
    _BUSINESS_ANCHOR_MAX,
    _extract_business_anchor,
    _extract_business_anchors,
    _strip_korean_particle,
)


# ---------------------------------------------------------------------------
# _strip_korean_particle — 한글 1자 조사 제거
# ---------------------------------------------------------------------------

def test_strip_particle_removes_common_particles() -> None:
    assert _strip_korean_particle("장애수당과") == "장애수당"
    assert _strip_korean_particle("기초연금은") == "기초연금"
    assert _strip_korean_particle("아동수당이") == "아동수당"
    assert _strip_korean_particle("장애의") == "장애"


def test_strip_particle_keeps_short_tokens() -> None:
    """짧은(<3) 토큰은 손상 방지를 위해 조사 제거 안 함."""
    assert _strip_korean_particle("과") == "과"
    assert _strip_korean_particle("의") == "의"
    assert _strip_korean_particle("을") == "을"


def test_strip_particle_keeps_no_particle_tokens() -> None:
    assert _strip_korean_particle("장애수당") == "장애수당"
    assert _strip_korean_particle("기초연금") == "기초연금"


# ---------------------------------------------------------------------------
# _extract_business_anchors — 복수 anchor 추출 (핵심)
# ---------------------------------------------------------------------------

def test_anchors_returns_both_substring_business_names() -> None:
    """가장 중요한 회귀 — "장애수당" 과 "장애아동수당" 둘 다 추출되어야 함."""
    anchors = _extract_business_anchors(
        vector="합천 장애수당과 장애아동수당 차이",
        keyword="합천 장애 수당 아동 차이",
        sigun_filters=["경상남도 합천군"],
    )
    assert "장애아동수당" in anchors
    assert "장애수당" in anchors


def test_anchors_returns_single_when_only_one_business_name() -> None:
    """기존 동작 유지 — 단일 사업명만 있으면 1개 반환."""
    anchors = _extract_business_anchors(
        vector="창원 기초연금 신청",
        keyword="창원 기초연금",
        sigun_filters=["경상남도 창원시"],
    )
    assert anchors == ["기초연금"]


def test_anchors_excludes_sigun_and_stopwords() -> None:
    """시군명·일반어("지원/복지/사업/서비스")는 anchor 후보에서 제외."""
    anchors = _extract_business_anchors(
        vector="창원 복지 서비스 지원",
        keyword="창원 복지 서비스",
        sigun_filters=["경상남도 창원시"],
    )
    # 모두 banned → anchor 없음
    assert anchors == []


def test_anchors_excludes_numeric_tokens() -> None:
    """연도/숫자 토큰은 anchor 후보에서 제외."""
    anchors = _extract_business_anchors(
        vector="2026 기초연금 신청",
        keyword="2026 기초연금",
        sigun_filters=None,
    )
    assert "2026" not in anchors
    assert "기초연금" in anchors


def test_anchors_caps_at_max() -> None:
    """과도한 anchor 수를 막기 위한 cap (WHERE 트리 폭증 방지)."""
    anchors = _extract_business_anchors(
        vector="기초연금 노인일자리 노인맞춤돌봄 장애수당 장애아동수당",
        keyword="기초연금 노인일자리 노인맞춤돌봄 장애수당 장애아동수당",
        sigun_filters=None,
    )
    assert len(anchors) <= _BUSINESS_ANCHOR_MAX


def test_anchors_handles_particle_in_query() -> None:
    """조사 붙은 토큰("장애수당과")도 정상 처리되어 "장애수당" 으로 추출."""
    anchors = _extract_business_anchors(
        vector="장애수당과 장애아동수당",
        keyword="장애 수당 아동",
        sigun_filters=None,
    )
    assert "장애수당" in anchors
    # 조사 그대로 남아 있으면 안 됨
    assert "장애수당과" not in anchors


def test_anchors_common_tokens_preferred() -> None:
    """vector·keyword 공통 토큰이 vector 단독보다 우선."""
    anchors = _extract_business_anchors(
        vector="기초연금 노인일자리 추가내용",
        keyword="기초연금",   # 공통: "기초연금"
        sigun_filters=None,
    )
    assert anchors[0] == "기초연금"


def test_anchors_empty_input() -> None:
    assert _extract_business_anchors("", "", None) == []
    assert _extract_business_anchors(" ", " ", None) == []


# ---------------------------------------------------------------------------
# _extract_business_anchor — 단수 wrapper 호환성
# ---------------------------------------------------------------------------

def test_single_anchor_wrapper_returns_first() -> None:
    """기존 단수 함수는 복수 후보 중 첫 번째 반환."""
    result = _extract_business_anchor(
        vector="장애수당과 장애아동수당",
        keyword="장애 수당 아동",
        sigun_filters=None,
    )
    # 가장 긴 substring 사업명이 우선 (장애아동수당)
    assert result == "장애아동수당"


def test_single_anchor_wrapper_returns_empty_when_no_candidate() -> None:
    assert _extract_business_anchor("창원 복지", "창원 복지", ["경상남도 창원시"]) == ""
