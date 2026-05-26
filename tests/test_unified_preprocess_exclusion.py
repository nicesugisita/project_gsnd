"""unified_preprocess 의 작업 6 — 배제 의도 분석 필드 파싱 단위 테스트.

LLM 호출(call_classifier_with_fallback)을 monkeypatch로 가짜 JSON 응답으로 대체.
실제 32B 엔드포인트는 호출하지 않는다.
"""

from __future__ import annotations

import json
from typing import Any, Dict

import pytest

from app.chat import preprocessing as pp
from app.chat.preprocessing import (
    _normalize_exclusion_intent,
    _normalize_str_list,
    unified_preprocess,
)


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------


def _make_parsed(
    *,
    intent: str = "general",
    query: str = "테스트",
    reformed: str = "테스트",
    exclusion_intent: str = "NONE",
    vector_query: str = "",
    must_not_keywords=None,
    anchor_entities=None,
    extra: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    parsed = {
        "query": query,
        "intent": intent,
        "intent_reason": "test",
        "reformed_query": reformed,
        "expanded_queries": [reformed],
        "keywords": [],
        "search_target": None,
        "policy_priority_tag": None,
        "detail_requested": False,
        "exclusion_intent": exclusion_intent,
        "vector_query": vector_query or reformed,
        "must_not_keywords": must_not_keywords or [],
        "anchor_entities": anchor_entities or [],
    }
    if extra:
        parsed.update(extra)
    return parsed


def _patch_llm(monkeypatch, parsed_dict):
    """call_classifier_with_fallback 을 가짜 응답으로 대체.

    반환 형식: (parsed_dict_or_None, raw_text, used_fallback_to_32b)
    """

    async def _fake(**kwargs):
        return parsed_dict, json.dumps(parsed_dict, ensure_ascii=False), False

    monkeypatch.setattr(pp, "call_classifier_with_fallback", _fake)


# ---------------------------------------------------------------------------
# 정규화 함수 단위 테스트
# ---------------------------------------------------------------------------


def test_normalize_exclusion_intent_known_values():
    for v in ("NONE", "PURE_EXCLUSION", "ANCHOR_COMPARISON",
              "RESIDUAL_CATEGORY", "SUBSTITUTION"):
        assert _normalize_exclusion_intent(v) == v


def test_normalize_exclusion_intent_case_insensitive_and_strip():
    assert _normalize_exclusion_intent(" anchor_comparison ") == "ANCHOR_COMPARISON"


def test_normalize_exclusion_intent_none_and_unknown_fallback():
    assert _normalize_exclusion_intent(None) == "NONE"
    assert _normalize_exclusion_intent("WEIRD") == "NONE"
    assert _normalize_exclusion_intent("") == "NONE"


def test_normalize_str_list_strips_and_dedupes():
    assert _normalize_str_list(["a", " b ", "", None, "a"]) == ["a", "b"]


def test_normalize_str_list_non_list_returns_empty():
    assert _normalize_str_list("not a list") == []
    assert _normalize_str_list(None) == []
    assert _normalize_str_list(123) == []


# ---------------------------------------------------------------------------
# unified_preprocess 통합 테스트
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unified_passes_through_new_fields(monkeypatch):
    _patch_llm(monkeypatch, _make_parsed(
        exclusion_intent="PURE_EXCLUSION",
        vector_query="SK하이닉스 영업이익",
        must_not_keywords=["삼성전자"],
    ))
    r = await unified_preprocess("삼성전자 빼고 SK하이닉스 영업이익", messages=[], use_rag=False)
    assert r["exclusion_intent"] == "PURE_EXCLUSION"
    assert r["vector_query"] == "SK하이닉스 영업이익"
    assert r["must_not_keywords"] == ["삼성전자"]
    assert r["anchor_entities"] == []


@pytest.mark.asyncio
async def test_anchor_comparison_keeps_anchor_drops_must_not(monkeypatch):
    """ANCHOR_COMPARISON: anchor_entities 유지, must_not 강제 비움."""
    _patch_llm(monkeypatch, _make_parsed(
        exclusion_intent="ANCHOR_COMPARISON",
        vector_query="기초연금 비슷한 노인 지원",
        anchor_entities=["기초연금"],
        must_not_keywords=["기초연금"],  # LLM 실수로 채웠다고 가정
    ))
    r = await unified_preprocess("기초연금 말고 비슷한 노인 지원", messages=[], use_rag=False)
    assert r["exclusion_intent"] == "ANCHOR_COMPARISON"
    assert r["must_not_keywords"] == []  # 강제 비움
    assert r["anchor_entities"] == ["기초연금"]


@pytest.mark.asyncio
async def test_non_anchor_drops_anchor_entities(monkeypatch):
    """ANCHOR 외에는 anchor_entities 무조건 [] 로 강제."""
    _patch_llm(monkeypatch, _make_parsed(
        exclusion_intent="PURE_EXCLUSION",
        vector_query="x",
        must_not_keywords=["foo"],
        anchor_entities=["foo"],  # LLM 실수로 채웠다고 가정
    ))
    r = await unified_preprocess("foo 빼고 x", messages=[], use_rag=False)
    assert r["anchor_entities"] == []


@pytest.mark.asyncio
async def test_missing_new_fields_safe_defaults(monkeypatch):
    """expand 모드 등 새 필드가 LLM JSON에 없을 때 안전 default 적용."""
    parsed = {
        "query": "테스트",
        "intent": "general",
        "intent_reason": "",
        "reformed_query": "테스트 쿼리",
        "expanded_queries": ["테스트 쿼리"],
        "keywords": [],
        # exclusion_intent / vector_query / must_not_keywords / anchor_entities 누락
    }
    _patch_llm(monkeypatch, parsed)
    r = await unified_preprocess("테스트", messages=[], use_rag=False)
    assert r["exclusion_intent"] == "NONE"
    # vector_query 누락 → reformed_query 로 폴백
    assert r["vector_query"] == "테스트 쿼리"
    assert r["must_not_keywords"] == []
    assert r["anchor_entities"] == []


@pytest.mark.asyncio
async def test_camelcase_field_names_accepted(monkeypatch):
    """LLM 이 카멜케이스로 응답해도 정상 파싱돼야 함."""
    parsed = {
        "query": "테스트",
        "intent": "general",
        "intent_reason": "",
        "reformed_query": "테스트",
        "expanded_queries": ["테스트"],
        "exclusionIntent": "RESIDUAL_CATEGORY",
        "vectorQuery": "기타 노인 혜택",
        "mustNotKeywords": ["기초연금"],
        "anchorEntities": [],
    }
    _patch_llm(monkeypatch, parsed)
    r = await unified_preprocess("기초연금 외 노인 혜택", messages=[], use_rag=False)
    assert r["exclusion_intent"] == "RESIDUAL_CATEGORY"
    assert r["vector_query"] == "기타 노인 혜택"
    assert r["must_not_keywords"] == ["기초연금"]


@pytest.mark.asyncio
async def test_unknown_exclusion_intent_falls_back_to_none(monkeypatch):
    _patch_llm(monkeypatch, _make_parsed(
        exclusion_intent="WEIRD_NEW_INTENT",
        must_not_keywords=["X"],
    ))
    r = await unified_preprocess("질문", messages=[], use_rag=False)
    assert r["exclusion_intent"] == "NONE"


@pytest.mark.asyncio
async def test_llm_failure_fallback_includes_new_fields(monkeypatch):
    """LLM 호출이 None을 반환하면 _make_fallback 이 새 필드를 포함해야 함."""

    async def _fake(**kwargs):
        return None, "", False

    monkeypatch.setattr(pp, "call_classifier_with_fallback", _fake)
    r = await unified_preprocess("질문", messages=[], use_rag=False)
    assert r["exclusion_intent"] == "NONE"
    assert r["vector_query"] == "질문"
    assert r["must_not_keywords"] == []
    assert r["anchor_entities"] == []


# ---------------------------------------------------------------------------
# PreprocessResult dataclass 매핑 테스트
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_unified_preprocess_maps_new_fields(monkeypatch):
    """run_unified_preprocess 가 dict → PreprocessResult 변환 시 새 필드 매핑 보존."""
    from app.chat._pipeline_steps import run_unified_preprocess

    _patch_llm(monkeypatch, _make_parsed(
        exclusion_intent="SUBSTITUTION",
        vector_query="현물 지원",
        must_not_keywords=["현금"],
    ))
    pr = await run_unified_preprocess("현금 대신 현물 지원", messages=[], use_rag=False)
    assert pr.exclusion_intent == "SUBSTITUTION"
    assert pr.vector_query == "현물 지원"
    assert pr.must_not_keywords == ["현금"]
    assert pr.anchor_entities == []
