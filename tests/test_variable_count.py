"""guide_recommend 가변 개수 선택 순수 함수 테스트 (서버·DB 비의존)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app.chat.infra.rag.variable_count import (  # noqa: E402
    extract_topic_terms,
    topical_hit,
    select_variable_count,
)


def _doc(name, weight=0.0, rrf=None, content=""):
    d = {"BUSINESS_NAME": name, "WEIGHT": weight, "CONTENT": content}
    if rrf is not None:
        d["rrf_score"] = rrf
    return d


# ── extract_topic_terms ────────────────────────────────────────────────

def test_extract_topic_terms_drops_situational_and_region_and_numbers():
    kws = ["진주", "청년", "가구", "소득", "월세", "지원"]
    assert extract_topic_terms(kws, exclude=["진주"]) == ["월세"]


def test_extract_topic_terms_broad_query_yields_empty():
    # "노인 복지 추천" — 전부 상황·일반어 → 변별 주제어 없음
    assert extract_topic_terms(["노인", "복지", "추천"]) == []


def test_extract_topic_terms_dedup_and_numeric_filter():
    kws = ["월세", "월세", "200만", "1인", "관절"]
    assert extract_topic_terms(kws) == ["월세", "관절"]


# ── topical_hit ────────────────────────────────────────────────────────

def test_topical_hit_matches_name_and_content():
    d = _doc("주거안정 월세대출", content="월세 부담 완화")
    assert topical_hit(d, ["월세"]) == 1
    assert topical_hit(d, ["관절"]) == 0
    assert topical_hit(d, []) == 0


# ── select_variable_count: 좁은(주제어 있음) 질의 ────────────────────────

def test_narrow_query_keeps_only_hits_padded_to_min():
    docs = [
        _doc("주거안정 월세대출", weight=0.9, content="월세"),   # hit
        _doc("해외취업 지원", weight=0.8),                        # miss
        _doc("청년창업농장학금", weight=0.7),                     # miss
        _doc("청소년쉼터 운영", weight=0.6),                      # miss
    ]
    out = select_variable_count(docs, ["월세"], min_results=3, max_results=11)
    # hit 1개 + MIN(3) 보강 위해 점수 상위 miss 2개 = 3건
    assert len(out) == 3
    assert out[0]["BUSINESS_NAME"] == "주거안정 월세대출"
    # 보강은 점수 상위 miss 부터
    assert out[1]["BUSINESS_NAME"] == "해외취업 지원"


def test_narrow_query_all_hits_no_padding():
    docs = [
        _doc("월세대출", weight=0.9, content="월세"),
        _doc("청년 월세 특별지원", weight=0.8, content="월세"),
        _doc("주거 월세 보조", weight=0.7, content="월세"),
        _doc("해외취업 지원", weight=0.6),  # miss — 버려져야 함
    ]
    out = select_variable_count(docs, ["월세"], min_results=3, max_results=11)
    assert len(out) == 3
    assert all("월세" in d["BUSINESS_NAME"] or "월세" in d.get("CONTENT", "") for d in out)


def test_narrow_query_respects_max():
    docs = [_doc(f"월세 사업{i}", weight=1.0 - i * 0.01, content="월세") for i in range(15)]
    out = select_variable_count(docs, ["월세"], min_results=3, max_results=11)
    assert len(out) == 11


# ── select_variable_count: 광역(주제어 없음) 질의 ────────────────────────

def test_broad_query_score_tail_cut():
    # 점수 절벽: 상위 3건 후 급락 → 꼬리 컷
    docs = [
        _doc("노인 복지 A", weight=1.0),
        _doc("노인 복지 B", weight=0.95),
        _doc("노인 복지 C", weight=0.9),
        _doc("노인 복지 D", weight=0.2),  # 급락(0.9*0.6=0.54 미만) → 컷
        _doc("노인 복지 E", weight=0.1),
    ]
    out = select_variable_count(docs, [], keep_ratio=0.55, gap_drop=0.6, min_results=3, max_results=11)
    assert len(out) == 3


def test_broad_query_min_floor():
    docs = [
        _doc("A", weight=1.0),
        _doc("B", weight=0.1),  # 비율/급락으로 컷되지만 MIN 보장
        _doc("C", weight=0.05),
    ]
    out = select_variable_count(docs, [], min_results=3, max_results=11)
    assert len(out) == 3


# ── score 필드 선택 / 엣지 ───────────────────────────────────────────────

def test_uses_rrf_score_when_present():
    docs = [
        _doc("low weight high rrf", weight=0.1, rrf=0.9, content="월세"),
        _doc("high weight low rrf", weight=0.9, rrf=0.1, content="월세"),
    ]
    out = select_variable_count(docs, ["월세"], min_results=1, max_results=11)
    assert out[0]["BUSINESS_NAME"] == "low weight high rrf"


def test_empty_docs_returns_empty():
    assert select_variable_count([], ["월세"]) == []


def test_does_not_mutate_input():
    docs = [_doc("월세대출", weight=0.9, content="월세"), _doc("기타", weight=0.5)]
    snapshot = [dict(d) for d in docs]
    select_variable_count(docs, ["월세"], min_results=1)
    assert docs == snapshot
