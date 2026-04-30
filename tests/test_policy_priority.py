"""policy_priority · expansion_cap 단위 검증 (회귀용)."""

from app.chat.infra.rag.expansion_cap import dedupe_cap_expanded_queries
from app.chat.infra.rag.policy_priority import (
    apply_policy_priority_to_documents,
    augment_okms_dual_query,
    policy_extra_okms_searches,
    policy_supplement_welfare_queries,
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


def test_augment_okms_dual_elderly_vector_only():
    """트리플이 비어도 벡터에 정책 앵커 추가."""
    msg = "부모님 70대인데 받을 혜택이 있을까요?"
    vec, kw = augment_okms_dual_query(msg, "진주 거주 복지", "")
    assert "기초연금" in vec and "노인맞춤돌봄" in vec
    assert kw == ""


def test_augment_okms_dual_appends_kw_when_triple_present():
    msg = "부모님 70대인데 받을 혜택이 있을까요?"
    vec, kw = augment_okms_dual_query(msg, "진주", "복지 신청")
    assert "기초연금" in vec
    assert "노인맞춤돌봄" in kw or "맞춤돌봄" in kw


def test_policy_extra_okms_searches_pairs():
    msg = "어르신 70 세 혜택"
    pairs = policy_extra_okms_searches(msg, "진주 거주")
    assert len(pairs) == 2
    assert pairs[0][1] == "기초연금"
    assert "노인맞춤돌봄" in pairs[1][1]


def test_policy_supplement_welfare_queries():
    assert "기초연금" in policy_supplement_welfare_queries(
        "어르신에게 어떤 지원이 있나요?",
    )


def test_elderly_basic_pension_before_dolbom_despite_weight():
    """맞춤돌봄 등 여러 부스트어가 걸린 문서라도 기초연금 문서를 앞에 둔다."""
    msg = "부모님 70대인데 받을 혜택이 있을까요?"
    docs = [
        {
            "CHUNK_ID": "dolbom",
            "WEIGHT": 100,
            "NAME": "2026 창원시 노인맞춤돌봄서비스",
            "CONTENT": "노인맞춤돌봄 맞춤돌봄 돌봄서비스 안내",
        },
        {
            "CHUNK_ID": "basic",
            "WEIGHT": 1,
            "NAME": "기초연금",
            "CONTENT": "기초연금 신청 서류",
        },
    ]
    out = apply_policy_priority_to_documents(msg, docs, log_prefix="test")
    assert out[0]["CHUNK_ID"] == "basic"
    assert out[1]["CHUNK_ID"] == "dolbom"
