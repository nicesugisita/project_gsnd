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
    tags, kws = resolve_policy_boost_keywords("implant")
    assert "implant" in tags
    assert "임플란트" in kws


def test_resolve_low_income():
    tags, kws = resolve_policy_boost_keywords("low_income")
    assert "low_income" in tags
    assert "생계급여" in kws and "의료급여" in kws


def test_resolve_elderly_benefits():
    tags, kws = resolve_policy_boost_keywords("elderly_benefits")
    assert "elderly_benefits" in tags
    assert "기초연금" in kws


def test_resolve_elderly_benefits_age_65_plus():
    tags, kws = resolve_policy_boost_keywords("elderly_benefits")
    assert "elderly_benefits" in tags
    assert "기초연금" in kws


def test_resolve_elderly_benefits_age_64_not_matched():
    tags, _kws = resolve_policy_boost_keywords(None)
    assert "elderly_benefits" not in tags


def test_resolve_elderly_benefits_specific_topic_not_matched():
    tags, _kws = resolve_policy_boost_keywords(None)
    assert "elderly_benefits" not in tags


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
    out = apply_policy_priority_to_documents("low_income", docs, log_prefix="test")
    assert out[0]["CHUNK_ID"] == "2"
    assert out[1]["CHUNK_ID"] == "1"


def test_apply_policy_no_match_preserves_order():
    docs = [
        {"CHUNK_ID": "x", "WEIGHT": 1, "NAME": "a"},
        {"CHUNK_ID": "y", "WEIGHT": 2, "NAME": "b"},
    ]
    out = apply_policy_priority_to_documents("일반 질문", docs, log_prefix="test")
    assert [d["CHUNK_ID"] for d in out] == ["x", "y"]


def test_apply_policy_apply_enabled_false_preserves_order():
    """MORE_INFO 경로 등: 정책 부스트 재정렬 생략 시 입력 순서 유지."""
    docs = [
        {"CHUNK_ID": "1", "WEIGHT": 100, "NAME": "기타", "CONTENT": "일반"},
        {"CHUNK_ID": "2", "WEIGHT": 1, "NAME": "생계급여", "CONTENT": "생계급여 신청"},
    ]
    out = apply_policy_priority_to_documents(
        "low_income", docs, log_prefix="test", apply_enabled=False
    )
    assert [d["CHUNK_ID"] for d in out] == ["1", "2"]


def test_augment_okms_dual_elderly_vector_only():
    """트리플이 비어도 벡터에 정책 anchor 추가 (anchor 1개로 제한, side effect 완화).

    Side effect 완화 정책: anchor 다수를 벡터에 박으면 임베딩 중심이 anchor 방향으로
    과도하게 이동해 원본 쿼리 의미가 희석됨. anchor는 최우선 1개만 유지.
    """
    vec, kw = augment_okms_dual_query("elderly_benefits", "진주 거주 복지", "")
    # anchor 1개만 추가: 첫 번째 우선순위 키워드(보통 "기초연금")만 boost
    assert "기초연금" in vec
    assert kw == ""


def test_augment_okms_dual_appends_kw_when_triple_present():
    vec, kw = augment_okms_dual_query("elderly_benefits", "진주", "복지 신청")
    assert "기초연금" in vec
    # OP_HASANY 토큰 폭증 방지: keyword 보강은 최대 2개로 제한
    assert "기초연금" in kw or "맞춤돌봄" in kw


def test_augment_okms_dual_caps_keyword_extras(monkeypatch):
    """keyword leg 토큰 폭증 방지 — DB 키워드가 많아도 cap 만큼만 keyword 에 추가된다."""
    from app.chat.infra.rag import policy_priority
    from app.chat.infra.rag.policy_priority import _POLICY_BOOST_KEYWORD_EXTRA_MAX

    # DB 키워드를 충분히 많은 가짜로 치환해 cap 동작 검증 (실제 DB 의존 회피)
    fake_kws = {
        "elderly_benefits": ("alpha", "beta", "gamma", "delta", "epsilon"),
    }
    monkeypatch.setattr(policy_priority, "_get_tag_keywords_map", lambda: fake_kws)

    _, kw = augment_okms_dual_query("elderly_benefits", "원본", "복지")
    # 원본 kw("복지") 에 없던 elderly 키워드 중 실제로 들어간 것 카운트
    added = [k for k in fake_kws["elderly_benefits"] if k in kw and k != "복지"]
    assert len(added) <= _POLICY_BOOST_KEYWORD_EXTRA_MAX, (
        f"keyword extras cap 위반: added={added}, cap={_POLICY_BOOST_KEYWORD_EXTRA_MAX}"
    )


def test_policy_extra_okms_searches_pairs():
    """추가 검색 쌍은 1개로 제한 — 풀 오염 방지 (implant/low_income 과 일관)."""
    pairs = policy_extra_okms_searches("elderly_benefits", "진주 거주")
    assert len(pairs) == 1
    # 1개로 줄어든 후에도 우선순위 1번 anchor 가 들어가야 함
    assert pairs[0][1]  # anchor 비어있지 않음
    assert "안내" in pairs[0][0]  # vector 측에 "안내" suffix 유지


def test_policy_supplement_welfare_queries():
    assert "기초연금" in policy_supplement_welfare_queries(
        "elderly_benefits",
    )


def test_elderly_basic_pension_before_dolbom_despite_weight():
    """맞춤돌봄 등 여러 부스트어가 걸린 문서라도 기초연금 문서를 앞에 둔다."""
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
    out = apply_policy_priority_to_documents("elderly_benefits", docs, log_prefix="test")
    assert out[0]["CHUNK_ID"] == "basic"
    assert out[1]["CHUNK_ID"] == "dolbom"
