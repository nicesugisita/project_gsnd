"""ContextualQueryRewriter 단위 테스트.

LLM 호출(`app.chat.contextual_query_rewriter.call_llm_api`)을 monkeypatch로 가짜
응답으로 대체하여 5개 intent + 엣지케이스를 검증한다. 실제 32B 엔드포인트는 호출하지 않음.
"""

from __future__ import annotations

from datetime import date
import json

import pytest

from app.chat import contextual_query_rewriter as cqr
from app.chat.contextual_query_rewriter import (
    ExclusionIntent,
    QueryRewriteResult,
    rewrite_query,
)


def _make_llm_response(
    intent: str = "NONE",
    vector: str = "",
    llm: str = "",
    must_keywords=None,
    must_not_keywords=None,
    anchor_entities=None,
    has_exclusion=None,
) -> str:
    payload = {
        "exclusionIntent": intent,
        "vectorQuery": vector,
        "llmQuery": llm or vector,
        "mustKeywords": must_keywords or [],
        "mustNotKeywords": must_not_keywords or [],
        "anchorEntities": anchor_entities or [],
        "hasExclusion": has_exclusion if has_exclusion is not None else (intent != "NONE"),
    }
    return json.dumps(payload, ensure_ascii=False)


@pytest.fixture
def fixed_today(monkeypatch):
    return lambda: date(2026, 5, 21)


def _patch_llm(monkeypatch, response):
    """call_llm_api를 가짜 응답으로 대체. response가 callable이면 호출, 아니면 그대로 반환."""

    async def _fake_call_llm_api(**kwargs):
        if callable(response):
            return response(kwargs)
        return response

    monkeypatch.setattr(cqr, "call_llm_api", _fake_call_llm_api)


# ---------------------------------------------------------------------------
# NONE / 엣지케이스
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_none_self_evident_unchanged(monkeypatch, fixed_today):
    q = "Spring Boot Virtual Thread 사용법"
    _patch_llm(monkeypatch, _make_llm_response(intent="NONE", vector=q, llm=q))
    r = await rewrite_query(q, today_provider=fixed_today)
    assert r.exclusion_intent == ExclusionIntent.NONE
    assert r.vector_query == q
    assert r.must_not_keywords == []
    assert r.anchor_entities == []
    assert not r.has_exclusion
    assert not r.rewritten


@pytest.mark.asyncio
async def test_blank_question_skipped(monkeypatch, fixed_today):
    called = {"n": 0}

    async def _should_not_be_called(**kwargs):
        called["n"] += 1
        return ""

    monkeypatch.setattr(cqr, "call_llm_api", _should_not_be_called)
    r = await rewrite_query("   ", today_provider=fixed_today)
    assert called["n"] == 0
    assert isinstance(r, QueryRewriteResult)
    assert not r.rewritten


# ---------------------------------------------------------------------------
# PURE_EXCLUSION
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pure_exclusion_drops_entity_from_vec(monkeypatch, fixed_today):
    _patch_llm(monkeypatch, _make_llm_response(
        intent="PURE_EXCLUSION",
        vector="SK하이닉스 영업이익",
        llm="삼성전자 제외 SK하이닉스 영업이익",
        must_keywords=["SK하이닉스", "영업이익"],
        must_not_keywords=["삼성전자"],
    ))
    r = await rewrite_query("삼성전자 빼고 SK하이닉스 영업이익", today_provider=fixed_today)
    assert r.exclusion_intent == ExclusionIntent.PURE_EXCLUSION
    assert "삼성전자" not in r.vector_query
    assert r.must_not_keywords == ["삼성전자"]
    assert r.has_exclusion
    assert r.rewritten


# ---------------------------------------------------------------------------
# ANCHOR_COMPARISON
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anchor_keeps_entity_in_vec(monkeypatch, fixed_today):
    _patch_llm(monkeypatch, _make_llm_response(
        intent="ANCHOR_COMPARISON",
        vector="아반떼 비슷한 가격대 차종",
        llm="아반떼 제외 비슷한 가격대 다른 차종",
        anchor_entities=["아반떼"],
    ))
    r = await rewrite_query("아반떼 말고 비슷한 가격대 다른 차종", today_provider=fixed_today)
    assert r.exclusion_intent == ExclusionIntent.ANCHOR_COMPARISON
    assert "아반떼" in r.vector_query
    assert r.anchor_entities == ["아반떼"]
    assert r.must_not_keywords == []


@pytest.mark.asyncio
async def test_anchor_forces_empty_must_not_even_if_llm_fills(monkeypatch, fixed_today):
    """LLM이 ANCHOR에 mustNot을 잘못 채워 보내도 강제로 비워야 한다."""
    _patch_llm(monkeypatch, _make_llm_response(
        intent="ANCHOR_COMPARISON",
        vector="아반떼 비슷한 가격대",
        anchor_entities=["아반떼"],
        must_not_keywords=["아반떼"],  # LLM 실수
    ))
    r = await rewrite_query("아반떼 말고 비슷한 가격대", today_provider=fixed_today)
    assert r.must_not_keywords == []


# ---------------------------------------------------------------------------
# RESIDUAL_CATEGORY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_residual_drops_entity(monkeypatch, fixed_today):
    _patch_llm(monkeypatch, _make_llm_response(
        intent="RESIDUAL_CATEGORY",
        vector="기초생활수급자 기타 지원 혜택",
        llm="의료급여 제외 기타 지원",
        must_not_keywords=["의료급여"],
    ))
    r = await rewrite_query(
        "창녕군 기초생활수급자인데 의료급여 외 받을 수 있는 지원이 뭐가 있나요?",
        today_provider=fixed_today,
    )
    assert r.exclusion_intent == ExclusionIntent.RESIDUAL_CATEGORY
    assert "의료급여" not in r.vector_query
    assert r.must_not_keywords == ["의료급여"]


# ---------------------------------------------------------------------------
# SUBSTITUTION
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_substitution(monkeypatch, fixed_today):
    _patch_llm(monkeypatch, _make_llm_response(
        intent="SUBSTITUTION",
        vector="기아 판매량",
        llm="현대차 대신 기아 판매량",
        must_not_keywords=["현대차"],
    ))
    r = await rewrite_query("현대차 대신 기아 판매량", today_provider=fixed_today)
    assert r.exclusion_intent == ExclusionIntent.SUBSTITUTION
    assert "현대차" not in r.vector_query
    assert r.must_not_keywords == ["현대차"]


# ---------------------------------------------------------------------------
# Multi-turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_turn_ellipsis_carryover(monkeypatch, fixed_today):
    _patch_llm(monkeypatch, _make_llm_response(
        intent="NONE",
        vector="삼성전자 작년 매출",
        llm="삼성전자 작년 매출",
    ))
    history = [
        {"role": "user", "content": "삼성전자 2024년 매출 알려줘"},
        {"role": "assistant", "content": "300조원"},
    ]
    r = await rewrite_query("작년은?", messages=history, today_provider=fixed_today)
    assert "삼성전자" in r.vector_query


@pytest.mark.asyncio
async def test_date_context_injected_into_user_prompt(monkeypatch, fixed_today):
    captured = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return _make_llm_response(intent="NONE", vector="x", llm="x")

    monkeypatch.setattr(cqr, "call_llm_api", _capture)
    await rewrite_query("작년 매출", today_provider=fixed_today)
    user_msg = captured.get("message", "")
    assert "2025" in user_msg  # 2026-05-21 기준 작년 = 2025
    assert "Today: 2026-05-21" in user_msg


@pytest.mark.asyncio
async def test_multi_turn_picks_multi_turn_system_prompt(monkeypatch, fixed_today):
    captured = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return _make_llm_response(intent="NONE", vector="x", llm="x")

    monkeypatch.setattr(cqr, "call_llm_api", _capture)
    history = [
        {"role": "user", "content": "삼성전자 매출"},
        {"role": "assistant", "content": "300조"},
    ]
    await rewrite_query("작년은?", messages=history, today_provider=fixed_today)
    sys = captured.get("system_prompt", "")
    # multi-turn 프롬프트만 들어있는 문구
    assert "multi-turn conversation query rewriter" in sys
    # OUTPUT_SPEC도 같이 concat되어 있어야 함
    assert "EXCLUSION INTENT CLASSIFICATION" in sys


@pytest.mark.asyncio
async def test_single_turn_picks_single_turn_system_prompt(monkeypatch, fixed_today):
    captured = {}

    async def _capture(**kwargs):
        captured.update(kwargs)
        return _make_llm_response(intent="NONE", vector="x", llm="x")

    monkeypatch.setattr(cqr, "call_llm_api", _capture)
    await rewrite_query("그냥 질문", today_provider=fixed_today)
    sys = captured.get("system_prompt", "")
    assert "single-turn query intent disambiguator" in sys
    assert "EXCLUSION INTENT CLASSIFICATION" in sys


# ---------------------------------------------------------------------------
# Fallback / 폴백
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_exception_returns_default(monkeypatch, fixed_today):
    async def _boom(**kwargs):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(cqr, "call_llm_api", _boom)
    r = await rewrite_query("질문", today_provider=fixed_today)
    assert r.vector_query == "질문"
    assert r.llm_query == "질문"
    assert not r.rewritten
    assert r.exclusion_intent == ExclusionIntent.NONE


@pytest.mark.asyncio
async def test_json_parse_failure_returns_default(monkeypatch, fixed_today):
    _patch_llm(monkeypatch, "not a json at all")
    r = await rewrite_query("질문", today_provider=fixed_today)
    assert r.vector_query == "질문"
    assert not r.rewritten


@pytest.mark.asyncio
async def test_code_fence_json_is_parsed(monkeypatch, fixed_today):
    fenced = (
        "여기 결과입니다:\n"
        "```json\n"
        + _make_llm_response(intent="PURE_EXCLUSION", vector="B 매출",
                             llm="A 제외 B 매출", must_not_keywords=["A"])
        + "\n```"
    )
    _patch_llm(monkeypatch, fenced)
    r = await rewrite_query("A 말고 B 매출", today_provider=fixed_today)
    assert r.exclusion_intent == ExclusionIntent.PURE_EXCLUSION
    assert r.must_not_keywords == ["A"]


@pytest.mark.asyncio
async def test_strips_surrounding_quotes(monkeypatch, fixed_today):
    payload = {
        "exclusionIntent": "PURE_EXCLUSION",
        "vectorQuery": '"B 매출"',
        "llmQuery": "B 매출",
        "mustKeywords": [],
        "mustNotKeywords": ["A"],
        "anchorEntities": [],
        "hasExclusion": True,
    }
    _patch_llm(monkeypatch, json.dumps(payload, ensure_ascii=False))
    r = await rewrite_query("A 말고 B 매출", today_provider=fixed_today)
    assert r.vector_query == "B 매출"


@pytest.mark.asyncio
async def test_unknown_intent_falls_back_to_none(monkeypatch, fixed_today):
    payload = {
        "exclusionIntent": "WEIRD_NEW_INTENT",
        "vectorQuery": "x",
        "llmQuery": "x",
        "mustKeywords": [],
        "mustNotKeywords": [],
        "anchorEntities": [],
        "hasExclusion": False,
    }
    _patch_llm(monkeypatch, json.dumps(payload))
    r = await rewrite_query("질문", today_provider=fixed_today)
    assert r.exclusion_intent == ExclusionIntent.NONE
