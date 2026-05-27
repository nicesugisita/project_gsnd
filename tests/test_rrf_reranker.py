"""RRF reranker 인터페이스 단위 테스트."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from app.chat.infra.rag.rrf_reranker import (  # noqa: E402
    RRF_K,
    SCORE_RRF,
    rerank_by_rrf,
)


def _doc(chunk_id: str, **extra) -> dict:
    return {"CHUNK_ID": chunk_id, **extra}


def test_rrf_k_is_fixed_at_60():
    assert RRF_K == 60


def test_no_lists_returns_empty():
    assert rerank_by_rrf() == []


def test_all_empty_lists_returns_empty():
    assert rerank_by_rrf([], [], []) == []


def test_single_list_preserves_order_and_scores():
    docs = [_doc("a"), _doc("b"), _doc("c")]
    fused = rerank_by_rrf(docs)
    assert [d["CHUNK_ID"] for d in fused] == ["a", "b", "c"]
    assert fused[0][SCORE_RRF] == pytest.approx(1.0 / 61)
    assert fused[1][SCORE_RRF] == pytest.approx(1.0 / 62)
    assert fused[2][SCORE_RRF] == pytest.approx(1.0 / 63)


def test_two_lists_both_rank1_same_doc_wins():
    list_a = [_doc("x"), _doc("y"), _doc("z")]
    list_b = [_doc("x"), _doc("z"), _doc("y")]  # x도 1등
    fused = rerank_by_rrf(list_a, list_b)
    assert fused[0]["CHUNK_ID"] == "x"
    assert fused[0][SCORE_RRF] == pytest.approx(2.0 / 61)


def test_doc_only_in_one_list_gets_single_signal_score():
    list_a = [_doc("a"), _doc("b")]
    list_b = [_doc("c")]
    fused = rerank_by_rrf(list_a, list_b)
    by_id = {d["CHUNK_ID"]: d[SCORE_RRF] for d in fused}
    assert by_id["a"] == pytest.approx(1.0 / 61)
    assert by_id["b"] == pytest.approx(1.0 / 62)
    assert by_id["c"] == pytest.approx(1.0 / 61)


def test_variadic_three_lists_accumulate():
    fused = rerank_by_rrf(
        [_doc("x")],
        [_doc("x")],
        [_doc("x")],
    )
    assert len(fused) == 1
    assert fused[0][SCORE_RRF] == pytest.approx(3.0 / 61)


def test_first_occurrence_instance_is_kept():
    a = _doc("same", source="list_a")
    b = _doc("same", source="list_b")
    fused = rerank_by_rrf([a], [b])
    assert len(fused) == 1
    assert fused[0]["source"] == "list_a"


def test_weights_applied_per_list():
    list_a = [_doc("a")]
    list_b = [_doc("b")]
    fused = rerank_by_rrf(list_a, list_b, weights=[3.0, 1.0])
    by_id = {d["CHUNK_ID"]: d[SCORE_RRF] for d in fused}
    assert by_id["a"] == pytest.approx(3.0 / 61)
    assert by_id["b"] == pytest.approx(1.0 / 61)
    assert fused[0]["CHUNK_ID"] == "a"


def test_weights_length_mismatch_raises():
    with pytest.raises(ValueError):
        rerank_by_rrf([_doc("a")], [_doc("b")], weights=[1.0])


def test_top_k_truncates():
    list_a = [_doc(str(i)) for i in range(5)]
    fused = rerank_by_rrf(list_a, top_k=2)
    assert len(fused) == 2
    assert [d["CHUNK_ID"] for d in fused] == ["0", "1"]


def test_top_k_zero_returns_all():
    list_a = [_doc(str(i)) for i in range(5)]
    fused = rerank_by_rrf(list_a, top_k=0)
    assert len(fused) == 5


def test_empty_list_among_inputs_is_ignored():
    fused = rerank_by_rrf([_doc("a")], [], [_doc("b")])
    assert {d["CHUNK_ID"] for d in fused} == {"a", "b"}


def test_custom_key_function_dedupes_across_lists():
    list_a = [{"NAME": "policy_X"}]
    list_b = [{"NAME": "policy_X"}]
    fused = rerank_by_rrf(list_a, list_b, key=lambda d: d["NAME"])
    assert len(fused) == 1
    assert fused[0][SCORE_RRF] == pytest.approx(2.0 / 61)


def test_default_key_falls_back_to_id_then_object_identity():
    # CHUNK_ID 없고 ID만 있을 때 → ID로 매칭
    a1 = {"ID": "P1"}
    a2 = {"ID": "P1"}
    fused = rerank_by_rrf([a1], [a2])
    assert len(fused) == 1
    assert fused[0][SCORE_RRF] == pytest.approx(2.0 / 61)

    # 키 어느 것도 없으면 객체 동일성 → 별개 문서로 취급
    x = {"text": "foo"}
    y = {"text": "foo"}
    fused2 = rerank_by_rrf([x], [y])
    assert len(fused2) == 2


def test_realistic_two_signal_fusion_rank_combinations():
    # vector 결과: a, b, c
    # bm25  결과: c, a, b
    # a:  1/61 + 1/62, c: 1/63 + 1/61, b: 1/62 + 1/63
    vec = [_doc("a"), _doc("b"), _doc("c")]
    bm25 = [_doc("c"), _doc("a"), _doc("b")]
    fused = rerank_by_rrf(vec, bm25)
    by_id = {d["CHUNK_ID"]: d[SCORE_RRF] for d in fused}
    assert by_id["a"] == pytest.approx(1.0 / 61 + 1.0 / 62)
    assert by_id["c"] == pytest.approx(1.0 / 63 + 1.0 / 61)
    assert by_id["b"] == pytest.approx(1.0 / 62 + 1.0 / 63)
    # rank 합이 가장 작은 a(1+2)가 최상위, c(1+3)·b(2+3) 순
    assert [d["CHUNK_ID"] for d in fused] == ["a", "c", "b"]
