"""search 풀 라우팅 규칙 단위 테스트."""

from app.chat.infra.rag.pipeline_search import _search_pool_tag_from_target


def test_ambiguous_with_facility_hint_routes_to_welfare_center() -> None:
    assert _search_pool_tag_from_target("ambiguous") == "both"


def test_ambiguous_with_admin_hint_routes_to_our_region_tel() -> None:
    assert _search_pool_tag_from_target("") == "both"


def test_explicit_target_kept_even_with_message_hint() -> None:
    assert _search_pool_tag_from_target("admin_local_office") == "our_region_tel"
    assert _search_pool_tag_from_target("welfare_facility") == "welfare_center"
