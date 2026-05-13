"""LLM 호출 없이 intent 라우팅·휴리스틱을 검증하는 회귀 테스트.

리팩토링 로드맵 P0의 골든 케이스 일부.
- `_get_rag_processor`: intent → RAG processor 매핑
- `_prior_nonempty_assistant_exists`: NextIntent LLM 호출 스킵 조건
- `_normalize_search_target`: search_target 정규화
- `VALID_INTENTS` ↔ 프롬프트 ↔ 라우터의 정합성

LLM·DB·Mariner 모두 호출하지 않는 순수 단위 테스트.
"""
from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# _get_rag_processor — intent 분기 (현 상태 스냅샷; P3 라우터 테이블화 시 회귀 방지)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "intent, expected_attr",
    [
        ("general", "process_rag_general"),
        ("comparison", "process_rag_comparison"),
        ("guide_recommend", "process_rag_guide_recommend"),
        ("search", "process_rag_search"),
        ("unknown_intent", "process_rag_general"),  # fallback
        ("", "process_rag_general"),
    ],
)
def test_get_rag_processor_routes_to_expected_function(intent, expected_attr):
    from app.chat import _helpers
    from app.chat.infra.rag import (
        pipeline_comparison,
        pipeline_general,
        pipeline_guide_recommend,
        pipeline_search,
    )

    # 주의: comparison 은 pipeline_comparison.process_rag_with_documents_v2 가
    # `as process_rag_comparison` 으로 alias 되어 _helpers 에 import 됨
    expected_fn_map = {
        "process_rag_general": pipeline_general.process_rag_general,
        "process_rag_comparison": pipeline_comparison.process_rag_with_documents_v2,
        "process_rag_guide_recommend": pipeline_guide_recommend.process_rag_guide_recommend,
        "process_rag_search": pipeline_search.process_rag_search,
    }
    expected_fn = expected_fn_map[expected_attr]
    actual = _helpers._get_rag_processor(intent)
    assert actual is expected_fn, f"intent={intent!r} → {actual} (expected {expected_fn})"


def test_get_rag_processor_recommended_question_overrides_intent():
    """recommended_question_route=True 이면 intent와 무관하게 추천 질문 processor."""
    from app.chat import _helpers
    from app.chat.infra.rag.pipeline_recommended_question import (
        process_rag_recommended_question,
    )

    for intent in ("general", "comparison", "guide_recommend", "search"):
        result = _helpers._get_rag_processor(intent, recommended_question_route=True)
        assert result is process_rag_recommended_question


# ---------------------------------------------------------------------------
# NextIntent 휴리스틱 — _prior_nonempty_assistant_exists
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "messages, expected",
    [
        # 빈 이력 / 단일 메시지: False
        ([], False),
        ([{"role": "user", "content": "안녕"}], False),
        # 마지막 직전까지 assistant 가 비어 있음: False
        (
            [
                {"role": "assistant", "content": "   "},
                {"role": "user", "content": "질문"},
            ],
            False,
        ),
        # 비어있지 않은 assistant 가 존재: True
        (
            [
                {"role": "assistant", "content": "안녕하세요"},
                {"role": "user", "content": "질문"},
            ],
            True,
        ),
        # 중간에 assistant 내용 존재: True
        (
            [
                {"role": "user", "content": "안녕"},
                {"role": "assistant", "content": "답변"},
                {"role": "user", "content": "다시"},
            ],
            True,
        ),
    ],
)
def test_prior_nonempty_assistant_exists(messages, expected):
    from app.chat.routing import _prior_nonempty_assistant_exists

    assert _prior_nonempty_assistant_exists(messages) is expected


# ---------------------------------------------------------------------------
# build_next_intent_fallback — LLM 비활성·실패 시 폴백 일관성
# ---------------------------------------------------------------------------

def test_build_next_intent_fallback_returns_other_intent():
    from app.chat.routing import build_next_intent_fallback

    fallback, base = build_next_intent_fallback(
        messages=[{"role": "user", "content": "기초연금 알려줘"}],
        current_query="기초연금 알려줘",
    )
    assert fallback["intent"] == "OTHER"
    assert isinstance(fallback["re_query"], str) and fallback["re_query"]
    assert "llm_re_query" in fallback


def test_build_next_intent_fallback_uses_current_query_when_history_empty():
    from app.chat.routing import build_next_intent_fallback

    fallback, base = build_next_intent_fallback(messages=[], current_query="새질문")
    assert fallback["intent"] == "OTHER"
    assert fallback["re_query"] == "새질문"


# ---------------------------------------------------------------------------
# _normalize_search_target — intent와 무관한 표준화
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "intent, raw, expected",
    [
        # intent != search 인 경우 항상 None
        ("general", "admin_local_office", None),
        ("guide_recommend", "welfare_facility", None),
        ("comparison", "ambiguous", None),
        # intent == search 인 경우
        ("search", "admin_local_office", "admin_local_office"),
        ("search", "welfare_facility", "welfare_facility"),
        ("search", "ambiguous", "ambiguous"),
        # search 인데 raw 가 None → ambiguous
        ("search", None, "ambiguous"),
        # search 인데 알 수 없는 값 → ambiguous (경고 로그만)
        ("search", "unknown_value", "ambiguous"),
        # 하이픈 표기도 허용
        ("search", "admin-local-office", "admin_local_office"),
    ],
)
def test_normalize_search_target(intent, raw, expected):
    from app.chat.preprocessing import _normalize_search_target

    assert _normalize_search_target(intent, raw) == expected


# ---------------------------------------------------------------------------
# 정합성 — VALID_INTENTS, _get_rag_processor, 프롬프트 분류 라벨 일치
# ---------------------------------------------------------------------------

def test_valid_intents_all_have_processor():
    """VALID_INTENTS 의 모든 항목이 _get_rag_processor 에서 분기되어야 함."""
    from app.chat import _helpers
    from app.chat.preprocessing import VALID_INTENTS

    fallback_processor = _helpers._get_rag_processor("__definitely_not_an_intent__")
    for intent in VALID_INTENTS:
        proc = _helpers._get_rag_processor(intent)
        # general 은 fallback 과 동일 함수이지만, 다른 intent 는 fallback 과 달라야 함
        if intent == "general":
            assert proc is fallback_processor
        else:
            assert proc is not fallback_processor, f"{intent} → fallback과 동일"


def test_unified_preprocessing_prompt_mentions_all_intents():
    """프롬프트에 VALID_INTENTS 의 모든 라벨이 등장해야 분류가 가능."""
    from pathlib import Path

    from app.chat.preprocessing import VALID_INTENTS

    prompt = Path("prompts/unified_preprocessing_prompt.txt").read_text(encoding="utf-8")
    for intent in VALID_INTENTS:
        assert intent in prompt, f"프롬프트에 intent='{intent}' 라벨 누락"
