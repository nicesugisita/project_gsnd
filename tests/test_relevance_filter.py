from app.shared.utils.relevance_filter import (
    _contains_disability_term,
    _is_disability_related_doc,
)


def test_contains_disability_term_true_false():
    assert _contains_disability_term("장애인연금 가능한가요?") is True
    assert _contains_disability_term("노인 기초연금 가능한가요?") is False


def test_is_disability_related_doc_name_based():
    assert _is_disability_related_doc({"NAME": "장애인연금"}) is True
    assert _is_disability_related_doc({"NAME": "기초연금"}) is False


def test_is_disability_related_doc_ignores_non_title_fields():
    assert _is_disability_related_doc({"NAME": "기초연금", "CHUNK_PATH": "...장애인연금 안내..."}) is False
