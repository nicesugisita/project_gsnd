"""policy_priority · expansion_cap 단위 검증 (회귀용)."""

from app.chat.infra.rag.expansion_cap import dedupe_cap_expanded_queries
from app.chat.infra.rag.policy_priority import (
    apply_policy_priority_to_documents,
    resolve_policy_boost_keywords,
)


def test_resolve_implant_over_elderly():
    msg = "우리 부모님 70대인데, 진주에 거주 중입니다. 임플란트 지원을 받을 수 있나요?"
    tags, kws = resolve_policy_boost_keywords(msg)
    assert "implant" in tags
    assert "임플란트" in kws


def test_resolve_low_income():
    tags, kws = resolve_policy_boost_keywords("저소득 관련 지원은 무엇이 있나요?")
    assert "low_income" in tags
    assert "생계급여" in kws and "의료급여" in kws


def test_resolve_elderly_benefits():
    msg = "우리 부모님 70대인데, 진주에 거주 중일 때 받을 수 있는 혜택은?"
    tags, kws = resolve_policy_boost_keywords(msg)
    assert "elderly_benefits" in tags
    assert "기초연금" in kws


def test_dedupe_cap_order_and_max():
    rq = "기준"
    raw = ["a", "b", "a", rq, "c", "d"]
    out = dedupe_cap_expanded_queries(raw, max_n=3, reformed_query=rq)
    assert out == ["a", "b", "c"]


def test_apply_policy_reorders_by_keyword_hits():
    docs = [
        {"CHUNK_ID": "1", "WEIGHT": 100, "NAME": "기타", "CONTENT": "일반"},
        {"CHUNK_ID": "2", "WEIGHT": 1, "NAME": "생계급여", "CONTENT": "생계급여 신청"},
    ]
    out = apply_policy_priority_to_documents("저소득 지원", docs, log_prefix="test")
    assert out[0]["CHUNK_ID"] == "2"
    assert out[1]["CHUNK_ID"] == "1"


def test_apply_policy_no_match_preserves_order():
    docs = [
        {"CHUNK_ID": "x", "WEIGHT": 1, "NAME": "a"},
        {"CHUNK_ID": "y", "WEIGHT": 2, "NAME": "b"},
    ]
    out = apply_policy_priority_to_documents("일반 질문", docs, log_prefix="test")
    assert [d["CHUNK_ID"] for d in out] == ["x", "y"]
