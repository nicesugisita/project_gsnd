"""policy_priority_tag 룰 기반 fallback 단위 테스트."""

import pytest

from app.chat.infra.rag import policy_priority as pp


@pytest.fixture
def stub_keywords(monkeypatch):
    """DB 호출을 피하고 알려진 키워드 맵을 주입."""
    fake_map = {
        "implant": ("임플란트", "치과", "구강", "틀니"),
        "low_income": ("저소득", "생계급여", "의료급여", "기초생활"),
        "elderly_benefits": ("기초연금", "노인맞춤돌봄", "어르신"),
    }
    monkeypatch.setattr(pp, "_get_tag_keywords_map", lambda: fake_map)
    return fake_map


def test_empty_query_returns_none(stub_keywords):
    assert pp.infer_policy_priority_tag_by_keywords("") is None
    assert pp.infer_policy_priority_tag_by_keywords(None) is None


def test_no_keyword_match_returns_none(stub_keywords):
    assert pp.infer_policy_priority_tag_by_keywords("양산 청년 일자리 알려줘") is None


def test_elderly_keyword_matches(stub_keywords):
    assert pp.infer_policy_priority_tag_by_keywords("창원 노인 복지 추천") is None  # "노인"은 키워드에 없음
    assert pp.infer_policy_priority_tag_by_keywords("기초연금 신청 알려줘") == "elderly_benefits"
    assert pp.infer_policy_priority_tag_by_keywords("어르신 돌봄 서비스") == "elderly_benefits"


def test_implant_keyword_matches(stub_keywords):
    assert pp.infer_policy_priority_tag_by_keywords("임플란트 의료 지원") == "implant"
    assert pp.infer_policy_priority_tag_by_keywords("치과 진료비 지원") == "implant"


def test_low_income_keyword_matches(stub_keywords):
    assert pp.infer_policy_priority_tag_by_keywords("저소득 의료비 지원") == "low_income"
    assert pp.infer_policy_priority_tag_by_keywords("기초생활수급자 혜택") == "low_income"


def test_priority_implant_over_elderly(stub_keywords):
    # "어르신 임플란트" → implant 우선 (더 좁은 anchor)
    assert pp.infer_policy_priority_tag_by_keywords("어르신 임플란트 지원") == "implant"


def test_priority_low_income_over_elderly(stub_keywords):
    # "저소득 어르신" → low_income 우선
    assert pp.infer_policy_priority_tag_by_keywords("저소득 어르신 지원") == "low_income"


def test_empty_keyword_map_returns_none(monkeypatch):
    monkeypatch.setattr(pp, "_get_tag_keywords_map", lambda: {})
    assert pp.infer_policy_priority_tag_by_keywords("임플란트") is None


def test_keyword_map_load_exception_returns_none(monkeypatch):
    def _raise():
        raise RuntimeError("db down")
    monkeypatch.setattr(pp, "_get_tag_keywords_map", _raise)
    assert pp.infer_policy_priority_tag_by_keywords("임플란트") is None
