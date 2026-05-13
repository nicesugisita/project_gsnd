"""IntentRegistry (P3 산출물) 단위 회귀 테스트.

새 intent 추가·메타데이터 수정 시 의도하지 않은 변화를 감지한다.
"""
from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# 등록 일관성
# ---------------------------------------------------------------------------

def test_registry_contains_all_known_intents():
    from app.chat.intent_registry import INTENT_REGISTRY, get_intent_names

    names = get_intent_names()
    assert set(names) == {"general", "comparison", "guide_recommend", "search"}
    # 키와 IntentSpec.name 이 일치
    for key, spec in INTENT_REGISTRY.items():
        assert key == spec.name


def test_default_intent_is_general():
    from app.chat.intent_registry import DEFAULT_INTENT_NAME, lookup

    assert DEFAULT_INTENT_NAME == "general"
    assert lookup("__nope__").name == "general"
    assert lookup(None).name == "general"
    assert lookup("").name == "general"


# ---------------------------------------------------------------------------
# 메타데이터 스냅샷 (P3 도입 시 결정된 정책의 회귀 방지)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "intent, expected_lifecycle, expected_detail_form, expected_reuse_label",
    [
        ("general",         False, True,  ""),
        ("comparison",      False, False, ""),
        ("guide_recommend", True,  False, "guide_recommend"),
        ("search",          False, False, ""),
    ],
)
def test_intent_metadata_snapshot(
    intent, expected_lifecycle, expected_detail_form, expected_reuse_label
):
    from app.chat.intent_registry import lookup

    spec = lookup(intent)
    assert spec.requires_lifecycle is expected_lifecycle
    assert spec.supports_detail_form is expected_detail_form
    assert spec.reuse_label_on_more_info == expected_reuse_label


# ---------------------------------------------------------------------------
# Processor lazy 결정 — 호출되어도 import 사이클 없음
# ---------------------------------------------------------------------------

def test_processor_provider_resolves_without_cycle():
    from app.chat.intent_registry import INTENT_REGISTRY

    for spec in INTENT_REGISTRY.values():
        proc = spec.processor
        assert callable(proc)


def test_recommended_question_spec_resolves():
    from app.chat.intent_registry import RECOMMENDED_QUESTION_SPEC

    proc = RECOMMENDED_QUESTION_SPEC.processor
    assert callable(proc)
    assert proc.__name__ == "process_rag_recommended_question"


# ---------------------------------------------------------------------------
# resolve_reused_intent_on_more — MORE_INFO/MORE_DETAIL 매핑 정책
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "prior_intent, more_detail, expected",
    [
        # MORE_INFO: 모든 경우 guide_recommend 로 묶음
        ("general",         False, "guide_recommend"),
        ("search",          False, "guide_recommend"),
        ("comparison",      False, "guide_recommend"),
        ("guide_recommend", False, "guide_recommend"),
        (None,              False, "guide_recommend"),
        # MORE_DETAIL: search 는 사용자 발화에 연락처/주소 keyword 가 있을 때만 유지,
        # 그 외(빈 발화 포함)는 모두 general 로 좁힘.
        # search 정책: 시설 + 위치/연락처 류 전용 (운영시간/서비스 디테일은 general).
        ("search",          True,  "general"),  # user_message 빈 경우 → general 폴백
        ("general",         True,  "general"),
        ("guide_recommend", True,  "general"),
        ("comparison",      True,  "general"),
        (None,              True,  "general"),
        ("__unknown__",     True,  "general"),  # 미등록 → 기본 general
    ],
)
def test_resolve_reused_intent_on_more(prior_intent, more_detail, expected):
    from app.chat.intent_registry import resolve_reused_intent_on_more

    assert resolve_reused_intent_on_more(prior_intent, more_detail=more_detail) == expected


def test_resolve_reused_intent_on_more_search_with_contact_keyword():
    """user_message 에 연락처/주소 keyword 가 있을 때만 search 유지."""
    from app.chat.intent_registry import resolve_reused_intent_on_more

    # 연락처 keyword 있음 → search 유지
    assert resolve_reused_intent_on_more(
        "search", more_detail=True,
        user_message="가야읍 행정복지센터 전화번호",
    ) == "search"
    # 연락처 keyword 없음 (사업 디테일 요청) → general
    assert resolve_reused_intent_on_more(
        "search", more_detail=True,
        user_message="장애아동수당 자세히",
    ) == "general"


# ---------------------------------------------------------------------------
# normalize_intent — 미등록 입력 안전 처리
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("general", "general"),
        ("search", "search"),
        ("unknown", "general"),
        (None, "general"),
        ("", "general"),
    ],
)
def test_normalize_intent(raw, expected):
    from app.chat.intent_registry import normalize_intent

    assert normalize_intent(raw) == expected


# ---------------------------------------------------------------------------
# preprocessing.VALID_INTENTS 정합성 (P3에서 registry 기반으로 전환됨)
# ---------------------------------------------------------------------------

def test_preprocessing_valid_intents_matches_registry():
    from app.chat.intent_registry import get_intent_names
    from app.chat.preprocessing import VALID_INTENTS

    assert tuple(VALID_INTENTS) == get_intent_names()
