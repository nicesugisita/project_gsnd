"""queryset_welfare — JVM 없는 단위 검증."""

from app.mariner.queryset_welfare import _keywords_for_welfare_center


def test_keywords_merge_space_variants() -> None:
    k = "창원도우누리 노인통합재가센터"
    merged = _keywords_for_welfare_center(k)
    assert "창원도우누리" in merged
    assert "창원도우누리노인통합재가센터" in merged


def test_keywords_truncates_pipeline_garbage() -> None:
    blob = (
        "창원도우누리 창원도우누리노인 노인 노인통합 통합 통합재가 재가 재가센터 "
        "센터 센터 연락처 연락처"
    )
    merged = _keywords_for_welfare_center(blob)
    assert len(merged) <= 600
    assert "창원도우누리" in merged


def test_keywords_drops_contact_tail_words() -> None:
    merged = _keywords_for_welfare_center("창원도우누리 노인통합재가센터 연락처")
    assert "연락처" not in merged


def test_keywords_normalizes_dou_nuri_space() -> None:
    merged = _keywords_for_welfare_center("창원 도우 누리 노인통합재가센터 전화번호")
    assert "도우누리" in merged
    assert "도우 누리" not in merged
