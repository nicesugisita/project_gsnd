"""GOV_OKMS 검색 문자열의 일반어(stopword) 필터 회귀 테스트.

배경:
- GOV_OKMS WHERE 의 SERVICE_NAME_KO OP_HASANY(가중치 0.7) 가 한 토큰이라도
  매칭되면 boost 하므로, "복지/지원/서비스" 같은 광범위 일반어가 검색 문자열에
  포함되면 무관 서비스(예: "창원 노인 복지 추천" → "청소년복지시설 운영 지원")가
  상위에 진입하는 노이즈를 유발한다.
- pipeline_utils._filter_gov_okms_stopwords 가 이를 제거한다.
- 단, 전체 토큰이 stopword 인 경우엔 검색 신호를 보존하기 위해 원본을 반환한다.
"""
from __future__ import annotations

import pytest


def test_stopword_filter_removes_generic_tokens() -> None:
    from app.chat.infra.rag.pipeline_utils import _filter_gov_okms_stopwords

    out = _filter_gov_okms_stopwords("창원 노인 복지 추천")
    # "복지", "추천" 제거 → "창원 노인" 만 남아야 함
    assert out == "창원 노인", f"stopword 제거 결과 mismatch: {out!r}"


def test_stopword_filter_preserves_meaningful_tokens() -> None:
    from app.chat.infra.rag.pipeline_utils import _filter_gov_okms_stopwords

    out = _filter_gov_okms_stopwords("진주 치매 검사")
    # 변별 키워드(치매, 검사)는 stopword 아님 → 그대로 유지
    assert out == "진주 치매 검사"


def test_stopword_filter_keeps_original_when_all_tokens_are_stopwords() -> None:
    """전 토큰이 stopword 면 원본 반환(검색 신호 완전 소실 방지)."""
    from app.chat.infra.rag.pipeline_utils import _filter_gov_okms_stopwords

    out = _filter_gov_okms_stopwords("복지 지원 서비스")
    # 전부 stopword → 원본 그대로 반환
    assert out == "복지 지원 서비스"


def test_stopword_filter_handles_empty_input() -> None:
    from app.chat.infra.rag.pipeline_utils import _filter_gov_okms_stopwords

    assert _filter_gov_okms_stopwords("") == ""
    assert _filter_gov_okms_stopwords("   ") == ""


def test_stopword_filter_handles_partial_overlap() -> None:
    """변별 키워드 + 일반어 혼재 → 일반어만 제거."""
    from app.chat.infra.rag.pipeline_utils import _filter_gov_okms_stopwords

    out = _filter_gov_okms_stopwords("창원시 청년 일자리 지원 사업")
    # "지원", "사업" 제거 → "창원시 청년 일자리" 남음
    assert out == "창원시 청년 일자리"


def test_stopword_set_covers_expected_generics() -> None:
    """stopword 셋에 핵심 일반어가 포함되어 있어야 함 (회귀 보호)."""
    from app.chat.infra.rag.pipeline_utils import _GOV_OKMS_KEYWORD_STOPWORDS

    expected = {"복지", "지원", "서비스", "사업", "정책", "안내", "프로그램", "추천", "혜택"}
    missing = expected - set(_GOV_OKMS_KEYWORD_STOPWORDS)
    assert not missing, f"stopword 셋에 누락된 일반어: {missing}"
