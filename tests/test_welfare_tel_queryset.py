"""
GSND_OUR_REGION_TEL Mariner queryset — 단위 테스트 + 선택적 실서버 연동.

빠른 검증(기본):
  cd backend
  uv run pytest tests/test_welfare_tel_queryset.py -q

Mariner가 켜진 환경에서 창원·회원동 시나리오까지 확인:
  set WELFARE_TEL_MARINER_TEST=1
  uv run pytest tests/test_welfare_tel_queryset.py -q
"""

from __future__ import annotations

import os

import pytest

from app.mariner.queryset_welfare_tel import (
    _expand_sigun_scriptlet_values,
    _expand_ambiguous_eupmyeondong_filters,
    _eupmyeondong_filter_or_tokens,
)


def test_expand_sigun_changwon_includes_base_before_gu_suffixes() -> None:
    out = _expand_sigun_scriptlet_values(["경상남도 창원시"])
    assert out[0] == "경상남도 창원시"
    assert "창원시" in out
    assert len(out) == 2


def test_expand_sigun_unmapped_passes_through() -> None:
    assert _expand_sigun_scriptlet_values(["경상남도 진주시"]) == ["경상남도 진주시", "진주시"]


def test_eupmyeondong_filter_or_tokens_ambiguous_dong() -> None:
    toks = _eupmyeondong_filter_or_tokens("회원동")
    assert toks[0] == "회원동"
    assert "회원1동" in toks
    assert "회원9동" in toks
    assert len(toks) == 10


def test_eupmyeondong_filter_or_tokens_already_numbered() -> None:
    assert _eupmyeondong_filter_or_tokens("회원1동") == ["회원1동"]


def test_eupmyeondong_filter_or_tokens_eup_myeon() -> None:
    assert _eupmyeondong_filter_or_tokens("동읍") == ["동읍"]


def test_expand_ambiguous_eupmyeondong_filters_dedupes() -> None:
    out = _expand_ambiguous_eupmyeondong_filters(["회원동", "회원1동"])
    assert out[0] == "회원동"
    assert "회원1동" in out
    # 회원1동은 한 번만
    assert out.count("회원1동") == 1


def test_extract_eupmyeondong_matches_pipeline_for_hoewon() -> None:
    """pipeline_search가 넣는 eup 필터와 동일 추출기 — '동사무소'가 먼저 잡히지 않아야 함."""
    from app.chat.sigun import extract_eupmyeondong_from_message

    msg = "창원 회원동 동사무소 연락처"
    got = extract_eupmyeondong_from_message(msg)
    assert got == "회원동", f"expected 회원동, got {got!r}"


@pytest.mark.parametrize(
    "msg",
    [
        "양산 상북면행정복지센터 연락처",
        "양산 상북면 행정복지센터 연락처",
        "양산 상북면사무소 연락처",
        "양산 상북면 면사무소 연락처",
    ],
)
def test_extract_eupmyeondong_prefers_sangbukmyeon_over_bukmyeon(msg: str) -> None:
    from app.chat.sigun import extract_eupmyeondong_from_message

    got = extract_eupmyeondong_from_message(msg)
    assert got == "상북면", f"expected 상북면, got {got!r} for msg={msg!r}"


def test_check_sigun_first_turn_sangbukmyeon_does_not_fall_back_to_changwon() -> None:
    from app.chat.sigun import check_sigun

    query = "상북면 동사무소 연락처"
    messages = [
        {
            "role": "assistant",
            "content": "안녕하세요 경상남도청 복지챗봇입니다. 살고 계시는 지역과 함께 궁금한 복지서비스가 있다면 질문해 주세요.",
        },
        {"role": "user", "content": query},
    ]

    sigun_filters, need_clarify, _ = check_sigun(query, messages)
    assert need_clarify is False
    assert sigun_filters == ["경상남도 양산시"], f"unexpected sigun_filters: {sigun_filters!r}"


@pytest.mark.skipif(
    os.environ.get("WELFARE_TEL_MARINER_TEST", "").strip().lower() not in ("1", "true", "yes"),
    reason="실 Mariner 검색은 WELFARE_TEL_MARINER_TEST=1 일 때만 실행",
)
def test_mariner_live_hoewon_dong_office_contact() -> None:
    from app.core.config import Config
    from app.mariner.queryset_welfare_tel import query_welfare_tel_documents

    if not Config.RAG_ENABLED:
        pytest.skip("RAG_ENABLED=False")

    keyword = "창원"
    sigun = ["경상남도 창원시"]
    eup = ["회원동"]

    docs = query_welfare_tel_documents(
        keyword,
        sigun_filters=sigun,
        eupmyeondong_filters=eup,
        max_results=20,
    )
    eup_set = {str(d.get("EUPMYEONDONG") or "") for d in docs}
    assert "회원1동" in eup_set or "회원2동" in eup_set, f"unexpected EUPMYEONDONG set: {eup_set!r}"
