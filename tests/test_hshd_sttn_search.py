"""HOUSE_SITUATION(가구상황) 검색식 단위 검증.

검증 범위:
 1. extraction._extract_hshd_sttn_from_message  → (정규화값, 동의어) 추출
 2. queryset_okms._query_dual_documents         → WhereSet 안에 HSHD 필터/동의어 부스팅 entry 가
                                                  올바른 자리에 끼워졌는지 (JPype/Mariner 호출 없이 mock 으로 캡처)

JVM/Mariner 의존 없이 동작하도록 jpype 모듈을 sys.modules 에 fake 로 주입한다.
"""
from __future__ import annotations

import sys
import types
from typing import Any, List, Tuple
from unittest import mock

import pytest


# -------------------------------------------------------------------
# Fake jpype — queryset_okms 가 import 시점에 jpype 를 끌어오므로 미리 주입.
# -------------------------------------------------------------------
class _FakeJString(str):
    """JString(s) 가 str-호환 객체를 돌려주도록 흉내."""

    def __new__(cls, s: Any = ""):
        return super().__new__(cls, str(s))


def _install_fake_jpype() -> None:
    if "jpype" in sys.modules:
        return
    fake = types.ModuleType("jpype")

    def _jbyte(v: int) -> int:
        return int(v)

    class _JArray:
        def __init__(self, t):
            self._t = t

        def __call__(self, vals):
            return list(vals)

    fake.JString = _FakeJString  # type: ignore[attr-defined]
    fake.JByte = _jbyte  # type: ignore[attr-defined]
    fake.JArray = _JArray  # type: ignore[attr-defined]
    fake.JPackage = lambda name: types.SimpleNamespace()  # type: ignore[attr-defined]
    sys.modules["jpype"] = fake


_install_fake_jpype()


# -------------------------------------------------------------------
# 1. 추출 함수
# -------------------------------------------------------------------
def test_extract_low_income_keyword() -> None:
    from app.chat.infra.rag.extraction import _extract_hshd_sttn_from_message

    norm, syns = _extract_hshd_sttn_from_message("저소득 어르신 지원 알려줘")
    assert norm == "저소득"
    assert syns == ["기초생활수급자", "차상위", "생계급여", "의료급여", "주거급여"]


def test_extract_prefers_specific_keyword_over_generic() -> None:
    """'기초생활수급자' 가 입력되면 가장 긴 매칭 키가 잡혀야 한다 (수급자 단독이 아니라)."""
    from app.chat.infra.rag.extraction import (
        _HSHD_STTN_KEYWORD_MAP,
        _extract_hshd_sttn_from_message,
    )

    norm, _ = _extract_hshd_sttn_from_message("기초생활수급자 신청 방법")
    assert norm == "저소득"
    assert _HSHD_STTN_KEYWORD_MAP["기초생활수급자"] == "저소득"


def test_extract_space_variant_falls_back_to_short_match() -> None:
    """띄어쓰기가 있으면 긴 키워드는 substring 매칭이 안 되지만, 짧은 동의어로 잡혀야 한다."""
    from app.chat.infra.rag.extraction import _extract_hshd_sttn_from_message

    norm, _ = _extract_hshd_sttn_from_message("기초생활 수급자 신청")
    assert norm == "저소득"


def test_extract_no_match_returns_empty() -> None:
    from app.chat.infra.rag.extraction import _extract_hshd_sttn_from_message

    norm, syns = _extract_hshd_sttn_from_message("점심 메뉴 추천")
    assert norm == ""
    assert syns == []


def test_extract_other_categories() -> None:
    from app.chat.infra.rag.extraction import _extract_hshd_sttn_from_message

    assert _extract_hshd_sttn_from_message("한부모가정 지원")[0] == "한부모·조손"
    assert _extract_hshd_sttn_from_message("장애아동 돌봄")[0] == "장애인"
    assert _extract_hshd_sttn_from_message("다문화가정 자녀")[0] == "다문화·탈북민"
    assert _extract_hshd_sttn_from_message("다자녀 혜택")[0] == "다자녀"


def test_extract_veteran_category() -> None:
    """보훈대상자 — GOV_OKMS_V1 에 추가된 신규 값."""
    from app.chat.infra.rag.extraction import _extract_hshd_sttn_from_message

    norm, syns = _extract_hshd_sttn_from_message("국가유공자 지원 안내")
    assert norm == "보훈대상자"
    assert "국가유공자" in syns
    assert _extract_hshd_sttn_from_message("참전유공자")[0] == "보훈대상자"
    assert _extract_hshd_sttn_from_message("보훈가족")[0] == "보훈대상자"


# -------------------------------------------------------------------
# 2. WhereSet 구성 — JPype/Mariner 의존을 모두 mock 으로 가로채고
#    where_set_array 의 (field, op, value, weight) 시퀀스만 캡처해 검증.
# -------------------------------------------------------------------
class _CapturedWhereSet:
    """jpkg_query.WhereSet(...) 호출을 그대로 보관하는 더미."""

    def __init__(self, *args: Any) -> None:
        self.args: Tuple[Any, ...] = args

    def __repr__(self) -> str:
        return f"WS{self.args!r}"


def _make_query_dummy(captured_where: List[_CapturedWhereSet]):
    class _Q:
        def setResult(self, *a, **k) -> None: ...
        def setSearchKeyword(self, *a, **k) -> None: ...
        def setFrom(self, *a, **k) -> None: ...
        def setSearch(self, *a, **k) -> None: ...
        def setDebug(self, *a, **k) -> None: ...
        def setPrintQuery(self, *a, **k) -> None: ...
        def setLoggable(self, *a, **k) -> None: ...
        def setValue(self, *a, **k) -> None: ...
        def setSelect(self, *a, **k) -> None: ...
        def setOrderby(self, *a, **k) -> None: ...
        def setWhere(self, arr) -> None:
            captured_where.extend(arr)
        def setFilter(self, *a, **k) -> None: ...

    return _Q()


@pytest.fixture()
def captured_where() -> List[_CapturedWhereSet]:
    return []


def _build_fake_jpype_pieces(captured_where: List[_CapturedWhereSet]):
    """기본 JPype 인터페이스 흉내내는 fake 묶음 생성."""

    class _Pkg:
        Query = lambda self, *_a, **_k: _make_query_dummy(captured_where)
        QuerySet = lambda self, n: types.SimpleNamespace(addQuery=lambda *_a, **_k: None)
        SelectSet = lambda self, *_a, **_k: object()
        OrderBySet = lambda self, *_a, **_k: object()
        WhereSet = lambda self, *args: _CapturedWhereSet(*args)
        FilterSet = lambda self, *_a, **_k: object()

    class _CmdPkg:
        def CommandSearchRequest(self, *_a, **_k):
            class _Cmd:
                def setProps(self, *a, **k) -> None: ...
                def request(self, *a, **k) -> int:
                    return -1  # request 단계는 검증 대상 아님 → 음수로 즉시 중단

                def getResultSet(self):  # pragma: no cover
                    return None

            return _Cmd()

    _pkg_query = _Pkg()
    _cmd_pkg = _CmdPkg()

    def _jpackage(name: str):
        if "command" in name:
            return _cmd_pkg
        return _pkg_query

    fake_jpype = types.SimpleNamespace(
        JPackage=_jpackage,
        JByte=lambda v: int(v),
        JArray=lambda t: (lambda vals: list(vals)),
        JString=_FakeJString,
    )
    return fake_jpype


@pytest.fixture()
def patched_okms(monkeypatch, captured_where: List[_CapturedWhereSet]):
    """queryset_okms 의 JPype 의존을 모듈-레벨 심볼까지 모두 가로채고 캡처."""
    from app.mariner import queryset_okms as qo

    fake_jpype = _build_fake_jpype_pieces(captured_where)
    # queryset_okms 가 `import jpype` 와 `from jpype import JString` 둘 다 쓰므로
    # 모듈에 바인딩된 두 심볼을 동시에 monkeypatch 한다 (real JVM 호출 차단).
    monkeypatch.setattr(qo, "jpype", fake_jpype)
    monkeypatch.setattr(qo, "JString", _FakeJString)
    monkeypatch.setattr(qo, "ensure_jvm_thread", lambda: None)

    from app.core.config import Config

    patches = [
        mock.patch.object(Config, "RAG_ENABLED", True, create=True),
        mock.patch.object(Config, "MARINER_TIMEOUT", 10000, create=True),
        mock.patch.object(Config, "MARINER_THRESHOLD", 0.0, create=True),
        mock.patch.object(Config, "MARINER_MAX_RESULTS", 10, create=True),
        mock.patch.object(Config, "MARINER_IP", "127.0.0.1", create=True),
        mock.patch.object(Config, "MARINER_PORT", 0, create=True),
        mock.patch.object(Config, "RAG_COLLECTION", "TEST", create=True),
    ]
    for p in patches:
        p.start()
    yield qo
    for p in patches:
        p.stop()


def _ws_tuples(wsets: List[_CapturedWhereSet]) -> List[Tuple[Any, ...]]:
    return [w.args for w in wsets]


def _invoke_capture(patched_okms, **kwargs):
    """WhereSet 캡처 후 mocked request 의 -1 반환으로 raise 되는 RAGServiceError 는 무시."""
    from app.core.exceptions import RAGServiceError

    with pytest.raises(RAGServiceError):
        patched_okms.query_group_a_documents(**kwargs)


def test_whereset_includes_hshd_filter(patched_okms, captured_where) -> None:
    """hshd_sttn_filter='저소득' 전달 시 WhereSet 에 HOUSE_SITUATION op=34 entry 가 들어가야 한다."""
    _invoke_capture(
        patched_okms,
        vector="저소득 어르신",
        keyword="저소득 노인",
        collection="GSND_BIZ_DATASET_V4",
        hshd_sttn_filter="저소득",
        hshd_sttn_synonyms=None,
    )
    tuples = _ws_tuples(captured_where)
    hshd_entries = [t for t in tuples if len(t) >= 1 and t[0] == "HOUSE_SITUATION"]
    assert hshd_entries, f"HOUSE_SITUATION WhereSet 누락. 캡처={tuples}"
    field, op, value, weight = hshd_entries[0]
    assert op == 34, f"op=34(OP_HASANY|QUASI_SYNONYM) 기대, 실제={op}"
    assert str(value) == "저소득"
    assert weight == 0


def test_whereset_includes_synonym_boost(patched_okms, captured_where) -> None:
    """hshd_sttn_synonyms 전달 시 BUSINESS_NAME_KO/TEXT_CHUNK_KO 부스팅 entry 가 추가되어야 한다."""
    _invoke_capture(
        patched_okms,
        vector="저소득 어르신",
        keyword="저소득 노인",
        collection="GSND_BIZ_DATASET_V4",
        hshd_sttn_filter="저소득",
        hshd_sttn_synonyms=["기초생활수급자", "차상위", "생계급여", "의료급여", "주거급여"],
    )
    tuples = _ws_tuples(captured_where)
    # 동의어 토큰들이 공백 join 으로 BUSINESS_NAME_KO 에 들어갔는지 확인
    biz_ko_entries = [t for t in tuples if len(t) >= 3 and t[0] == "BUSINESS_NAME_KO"]
    # 메인 + 동의어 = 2개 이상이어야 함
    assert len(biz_ko_entries) >= 2, (
        f"BUSINESS_NAME_KO 부스팅 entry 가 추가되지 않음. 전체={tuples}"
    )
    # 동의어 토큰이 합쳐진 검색어가 한 entry 라도 존재해야 함
    syn_strings = [str(t[2]) for t in biz_ko_entries if " " in str(t[2])]
    assert any("기초생활수급자" in s and "차상위" in s for s in syn_strings), (
        f"동의어 join 문자열 누락. biz_ko entries={biz_ko_entries}"
    )


def test_whereset_no_hshd_filter_when_none(patched_okms, captured_where) -> None:
    """hshd_sttn_filter 미전달 시 HOUSE_SITUATION entry 가 들어가지 않아야 한다 (회귀 보호)."""
    _invoke_capture(
        patched_okms,
        vector="기초연금",
        keyword="기초연금",
        collection="GSND_BIZ_DATASET_V4",
    )
    tuples = _ws_tuples(captured_where)
    hshd_entries = [t for t in tuples if len(t) >= 1 and t[0] == "HOUSE_SITUATION"]
    assert not hshd_entries, f"HOUSE_SITUATION 이 의도치 않게 추가됨: {hshd_entries}"


# -------------------------------------------------------------------
# 3. GOV_OKMS WhereSet 구성 검증 (동일 패턴, queryset_gov_okms)
# -------------------------------------------------------------------
@pytest.fixture()
def patched_gov_okms(monkeypatch, captured_where: List[_CapturedWhereSet]):
    """queryset_gov_okms 의 JPype 의존을 모듈-레벨 심볼까지 모두 가로채고 캡처."""
    from app.mariner import queryset_gov_okms as qgo

    fake_jpype = _build_fake_jpype_pieces(captured_where)
    monkeypatch.setattr(qgo, "jpype", fake_jpype)
    monkeypatch.setattr(qgo, "JString", _FakeJString)
    monkeypatch.setattr(qgo, "ensure_jvm_thread", lambda: None)

    from app.core.config import Config

    patches = [
        mock.patch.object(Config, "RAG_ENABLED", True, create=True),
        mock.patch.object(Config, "MARINER_TIMEOUT", 10000, create=True),
        mock.patch.object(Config, "MARINER_THRESHOLD", 0.0, create=True),
        mock.patch.object(Config, "MARINER_IP", "127.0.0.1", create=True),
        mock.patch.object(Config, "MARINER_PORT", 0, create=True),
        mock.patch.object(Config, "RAG_GOV_OKMS_COLLECTION", "GOV_OKMS_V1", create=True),
    ]
    for p in patches:
        p.start()
    yield qgo
    for p in patches:
        p.stop()


def _invoke_gov(patched_gov_okms, **kwargs):
    from app.core.exceptions import RAGServiceError

    with pytest.raises(RAGServiceError):
        patched_gov_okms.query_gov_okms_documents(**kwargs)


def test_gov_okms_whereset_includes_house_situation_filter(
    patched_gov_okms, captured_where
) -> None:
    """GOV_OKMS 에 hshd_sttn_filter='저소득' 전달 시 HOUSE_SITUATION op=34 entry 추가."""
    _invoke_gov(
        patched_gov_okms,
        search_string="저소득 어르신",
        collection="GOV_OKMS_V1",
        hshd_sttn_filter="저소득",
        hshd_sttn_synonyms=None,
    )
    tuples = _ws_tuples(captured_where)
    hs_entries = [t for t in tuples if len(t) >= 1 and t[0] == "HOUSE_SITUATION"]
    assert hs_entries, f"GOV_OKMS HOUSE_SITUATION 누락. 캡처={tuples}"
    field, op, value, weight = hs_entries[0]
    assert op == 34
    assert str(value) == "저소득"


def test_gov_okms_whereset_includes_synonym_boost(patched_gov_okms, captured_where) -> None:
    """GOV_OKMS 에 hshd_sttn_synonyms 전달 시 SERVICE_NAME_KO/TEXT_CHUNK_KO 부스팅 entry 추가."""
    _invoke_gov(
        patched_gov_okms,
        search_string="저소득 어르신",
        collection="GOV_OKMS_V1",
        hshd_sttn_filter="저소득",
        hshd_sttn_synonyms=["기초생활수급자", "차상위", "생계급여", "의료급여", "주거급여"],
    )
    tuples = _ws_tuples(captured_where)
    svc_ko_entries = [t for t in tuples if len(t) >= 3 and t[0] == "SERVICE_NAME_KO"]
    assert len(svc_ko_entries) >= 2, (
        f"SERVICE_NAME_KO 부스팅 entry 추가 안 됨. 전체={tuples}"
    )
    syn_strings = [str(t[2]) for t in svc_ko_entries if " " in str(t[2])]
    assert any("기초생활수급자" in s and "차상위" in s for s in syn_strings), (
        f"동의어 join 문자열 누락. svc_ko entries={svc_ko_entries}"
    )


def test_gov_okms_whereset_no_filter_when_none(patched_gov_okms, captured_where) -> None:
    """GOV_OKMS 에 hshd_sttn_filter 미전달 시 HOUSE_SITUATION entry 가 없어야 한다."""
    _invoke_gov(
        patched_gov_okms,
        search_string="기초연금",
        collection="GOV_OKMS_V1",
    )
    tuples = _ws_tuples(captured_where)
    hs_entries = [t for t in tuples if len(t) >= 1 and t[0] == "HOUSE_SITUATION"]
    assert not hs_entries, f"HOUSE_SITUATION 이 의도치 않게 추가됨: {hs_entries}"


def test_synonym_token_cap(patched_okms, captured_where) -> None:
    """동의어가 캡(_HSHD_SYNONYMS_MAX_TOKENS=5)을 넘어도 5개까지만 합쳐져야 한다."""
    over_cap = [f"syn{i}" for i in range(12)]
    _invoke_capture(
        patched_okms,
        vector="저소득",
        keyword="저소득",
        collection="GSND_BIZ_DATASET_V4",
        hshd_sttn_filter="저소득",
        hshd_sttn_synonyms=over_cap,
    )
    tuples = _ws_tuples(captured_where)
    biz_ko_entries = [t for t in tuples if len(t) >= 3 and t[0] == "BUSINESS_NAME_KO"]
    syn_strings = [str(t[2]) for t in biz_ko_entries if "syn" in str(t[2])]
    assert syn_strings, "synonym entry 누락"
    # 모든 syn entry 가 cap 이내 토큰만 가져야 함
    for s in syn_strings:
        token_count = len([tok for tok in s.split() if tok])
        assert token_count <= patched_okms._HSHD_SYNONYMS_MAX_TOKENS, (
            f"동의어 토큰 캡 초과: {token_count} > {patched_okms._HSHD_SYNONYMS_MAX_TOKENS} ({s})"
        )
