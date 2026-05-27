"""stage_trace 단계 수집기 단위 테스트.

JVM 없이 검증하려고 WhereSet 을 흉내내는 FakeWhereSet 으로 introspection 을 친다.
실제 JPype char[] 는 문자 단위로 순회되므로, 필드/검색어를 char 리스트로 흉내낸다.
"""

import pytest

from app.chat.infra.rag import stage_trace as st


class FakeWhereSet:
    """JPype WhereSet 흉내. 연산자 전용이면 field/keywords 가 비어 있다.

    실제 Mariner WhereSet API 메서드명을 그대로 흉내낸다:
    getField()(1.6.3) / getOperation()(1.6.5) / getKeywords()(1.6.4) / getWeightRatio()(1.6.9).
    """

    def __init__(self, field="", op=0, keywords=None, weight=None):
        # 실제 char[] 처럼 문자 리스트로 보관 (introspection 변환 경로 검증)
        self._field = list(field or "")
        self._op = op
        # char[][] : 검색어마다 char 리스트
        self._keywords = [list(k) for k in (keywords or [])]
        self._weight = weight

    def getField(self):
        return self._field

    def getOperation(self):
        return self._op

    def getKeywords(self):
        return self._keywords

    def getWeightRatio(self):
        return [self._weight] if self._weight is not None else None


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    """수집기 토글을 강제로 켜고, 각 테스트 전 상태 초기화."""
    monkeypatch.setattr(st, "_is_enabled", lambda: True)
    st.reset()
    yield
    st.reset()


# ------------------------------------------------------------
# op 이름 매핑
# ------------------------------------------------------------


def test_op_name_exact_matches():
    assert st._op_name(2) == "HASANY"
    assert st._op_name(6) == "OR"
    assert st._op_name(96) == "VECTOR"
    assert st._op_name(8) == "WEIGHTAND"
    # 33 은 1|0x20 과 비트가 겹치지만 정확 일치(INT_SUMMATION)가 우선해야 함
    assert st._op_name(33) == "INT_SUMMATION"


def test_op_name_synonym_flag_decomposed():
    # 34 = HASANY(2) | QUASI_SYNONYM(0x20)
    assert st._op_name(34) == "HASANY|QUASI_SYN"
    # 18 = HASANY(2) | EQUIV_SYNONYM(0x10)
    assert st._op_name(18) == "HASANY|EQUIV_SYN"


def test_op_name_unknown():
    assert st._op_name(123).startswith("OP(")


# ------------------------------------------------------------
# WhereSet 재구성
# ------------------------------------------------------------


def _okms_vector_leg():
    """guide_recommend OKMS vector leg 의 5필드 OR + 필터 흉내."""
    ks = "노인 복지 서비스"
    return [
        FakeWhereSet(op=9),  # (
        FakeWhereSet("BUSINESS_NAME_KO", 2, [ks], weight=0.7),
        FakeWhereSet(op=6),  # OR
        FakeWhereSet("TEXT_CHUNK_KO", 2, [ks], weight=0.3),
        FakeWhereSet(op=6),
        FakeWhereSet("BUSINESS_NAME_MI", 2, [ks], weight=0.3),
        FakeWhereSet(op=6),
        FakeWhereSet("TEXT_CHUNK_MI", 96, [ks], weight=0.3),
        FakeWhereSet(op=6),
        FakeWhereSet("SIGUN", 96, [ks], weight=0.1),
        FakeWhereSet(op=10),  # )
        FakeWhereSet(op=5),  # AND
        FakeWhereSet("SIGUN", 33, ["창원"]),
        FakeWhereSet(op=5),
        FakeWhereSet("LIFE_CYCLE", 34, ["노년기"]),
        FakeWhereSet(op=8),  # WEIGHTAND
        FakeWhereSet("HOUSE_SITUATION", 34, ["일반가구"], weight=0.3),
    ]


def test_render_whereset_structure():
    expr = st.render_whereset(_okms_vector_leg())
    # op getter(getOperation) + 가중치(getWeightRatio) 가 함께 렌더돼야 함
    assert expr.startswith('( BUSINESS_NAME_KO[HASANY]="노인 복지 서비스"^0.7')
    assert 'TEXT_CHUNK_MI[VECTOR]="노인 복지 서비스"^0.3' in expr
    assert 'SIGUN[VECTOR]="노인 복지 서비스"^0.1' in expr
    assert ') AND SIGUN[INT_SUMMATION]="창원"' in expr
    assert 'LIFE_CYCLE[HASANY|QUASI_SYN]="노년기"' in expr
    assert 'WEIGHTAND HOUSE_SITUATION[HASANY|QUASI_SYN]="일반가구"^0.3' in expr
    # 연산자 WhereSet 은 이름만 (None 으로 새지 않아야 함)
    assert "None" not in expr


def test_render_whereset_empty():
    assert st.render_whereset([]) == ""
    assert st.render_whereset(None) == ""


def test_classify_clauses_separates_query_and_filters():
    query_terms, filters = st._classify_clauses(_okms_vector_leg())
    # 메인 검색쿼리: 5필드가 같은 검색어 공유 → 중복 제거되어 1종
    assert query_terms == ["노인 복지 서비스"]
    # 필터: 시군(INT_SUMMATION) / 생애주기 / 가구상황 — 검색쿼리와 분리
    fmap = {f["field"]: f["value"] for f in filters}
    assert fmap["SIGUN"] == "창원"
    assert fmap["LIFE_CYCLE"] == "노년기"
    assert fmap["HOUSE_SITUATION"] == "일반가구"
    # 메인 검색어가 필터로 새지 않아야 함
    assert "노인 복지 서비스" not in fmap.values()


def test_leg_from_label():
    assert st._leg_from_label("OKMS#0") == "keyword"
    assert st._leg_from_label("OKMS#1") == "vector"
    assert st._leg_from_label("GOV_OKMS") == "single"


# ------------------------------------------------------------
# 기록 / 스냅샷 라이프사이클
# ------------------------------------------------------------


def test_record_preprocess_and_snapshot():
    st.record_preprocess(
        {
            "intent": "guide_recommend",
            "reformed_query": "노인 복지 서비스",
            "expanded_queries": ["노인 복지", "어르신 지원"],
            "keywords": ["노인", "복지"],
            "policy_priority_tag": "elderly_benefits",
            "vector_query": "노인 복지 서비스",
        }
    )
    snap = st.snapshot()
    assert snap["intent"] == "guide_recommend"
    assert snap["reformed_query"] == "노인 복지 서비스"
    assert snap["expanded_queries"] == ["노인 복지", "어르신 지원"]
    assert snap["policy_priority_tag"] == "elderly_benefits"


def test_record_search_query_accumulates():
    st.record_search_query("OKMS#1", _okms_vector_leg())
    st.record_search_query("GOV_OKMS", [FakeWhereSet("SERVICE_NAME_KO", 2, ["노인"])])
    snap = st.snapshot()
    sq = snap["search_queries"]
    assert len(sq) == 2
    # OKMS#1 = vector leg, 메인 검색쿼리/필터 분리 기록
    assert sq[0]["label"] == "OKMS#1"
    assert sq[0]["leg"] == "vector"
    assert sq[0]["query"] == "노인 복지 서비스"
    assert {f["field"] for f in sq[0]["filters"]} == {"SIGUN", "LIFE_CYCLE", "HOUSE_SITUATION"}
    assert sq[0]["n_clauses"] == 17
    # GOV_OKMS = 단일, 필터 없음
    assert sq[1]["label"] == "GOV_OKMS"
    assert sq[1]["leg"] == "single"
    assert sq[1]["query"] == "노인"


def test_reset_clears_state():
    st.record_search_query("OKMS", _okms_vector_leg())
    st.reset()
    snap = st.snapshot()
    assert snap.get("search_queries") == []
    assert "intent" not in snap


def test_disabled_is_noop(monkeypatch):
    monkeypatch.setattr(st, "_is_enabled", lambda: False)
    st.reset()
    st.record_preprocess({"intent": "x"})
    st.record_search_query("OKMS", _okms_vector_leg())
    assert st.snapshot() == {}
