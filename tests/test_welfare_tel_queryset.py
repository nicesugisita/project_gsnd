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
    _augment_welfare_tel_search_keyword,
    _eupmyeondong_filter_or_tokens,
    _eupmyeondong_tokens_from_bare_place_keyword,
    _has_standalone_bokji_center_phrase,
    _merge_eup_ambiguous_numbered_dongs_into_keyword,
    _prepare_welfare_tel_mariner_keyword,
)


def test_expand_sigun_changwon_includes_base_before_gu_suffixes() -> None:
    out = _expand_sigun_scriptlet_values(["경상남도 창원시"])
    assert out[0] == "경상남도 창원시"
    assert "경상남도 창원시 의창구" in out
    assert "경상남도 창원시 진해구" in out
    assert len(out) == 1 + 5


def test_expand_sigun_unmapped_passes_through() -> None:
    assert _expand_sigun_scriptlet_values(["경상남도 진주시"]) == ["경상남도 진주시"]


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


def test_merge_adds_numbered_when_keyword_short() -> None:
    kw = _merge_eup_ambiguous_numbered_dongs_into_keyword("창원", ["회원동"])
    assert "창원" in kw
    assert "회원1동" in kw
    assert "회원2동" in kw


def test_prepare_keyword_pipeline_like_short_triple() -> None:
    """RAG Step2 이후 키워드만 '창원'처럼 짧게 오는 경우(동사무소 맥락 없음)."""
    prepared = _prepare_welfare_tel_mariner_keyword("창원", ["회원동"])
    assert "창원" in prepared
    assert "회원1동" in prepared


def test_prepare_full_user_phrase() -> None:
    prepared = _prepare_welfare_tel_mariner_keyword(
        "창원 회원동 동사무소 연락처",
        ["회원동"],
    )
    assert "창원" in prepared
    assert "회원1동" in prepared or "회원동" in prepared


def test_eup_tokens_from_bare_place_okpo() -> None:
    toks = _eupmyeondong_tokens_from_bare_place_keyword("거제시 옥포 주민센터 연락처")
    assert "옥포동" in toks
    assert "옥포1동" in toks
    assert "옥포2동" in toks


def test_eup_tokens_bare_deokgye_plain_dong() -> None:
    """덕계동은 번호 분동이 아니라 단일 행정동 이름."""
    toks = _eupmyeondong_tokens_from_bare_place_keyword("양산 덕계 행정복지센터 연락처")
    assert "덕계동" in toks


def test_augment_yangsan_deokgye_haengjeong() -> None:
    aug = _augment_welfare_tel_search_keyword("양산 덕계 행정복지센터 연락처")
    assert "덕계동" in aug


def test_eup_tokens_skips_city_name_before_jumin() -> None:
    """`거제시 주민센터`는 시명+시설만 있어 동 보강 대상이 아님."""
    assert _eupmyeondong_tokens_from_bare_place_keyword("거제시 주민센터 연락처") == []


def test_eup_tokens_skip_gyeongnam_city_short_before_dongsamuso() -> None:
    """`양산`은 경남 시 약칭 → `양산1동` 보강 금지(과매칭)."""
    assert _eupmyeondong_tokens_from_bare_place_keyword("양산 동사무소 연락처") == []


def test_standalone_bokji_center_phrase() -> None:
    assert _has_standalone_bokji_center_phrase("양산 복지센터")
    assert not _has_standalone_bokji_center_phrase("사회복지센터")
    assert not _has_standalone_bokji_center_phrase("양산 행정복지센터")


def test_augment_maps_bokji_center_to_haengjeong() -> None:
    aug = _augment_welfare_tel_search_keyword("양산 복지센터 연락처")
    assert "행정복지센터" in aug
    assert "행복복지센터" in aug


def test_prepare_geoje_okpo_includes_numbered_dongs() -> None:
    p = _prepare_welfare_tel_mariner_keyword("거제시 옥포 주민센터 연락처", None)
    assert "옥포1동" in p


def test_extract_eupmyeondong_matches_pipeline_for_hoewon() -> None:
    """pipeline_search가 넣는 eup 필터와 동일 추출기 — '동사무소'가 먼저 잡히지 않아야 함."""
    from app.chat.sigun import extract_eupmyeondong_from_message

    msg = "창원 회원동 동사무소 연락처"
    got = extract_eupmyeondong_from_message(msg)
    assert got == "회원동", f"expected 회원동, got {got!r}"


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
