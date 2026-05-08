"""queryset_gsnd — GSND_DATASET_V8 검색 단위 검증 + 선택적 실서버 검증.

빠른 검증(기본):
  c:/dev/gsnd_rag_v4/backend/.venv/Scripts/python.exe -m pytest tests/test_queryset_gsnd.py -q

Mariner 실서버 검증(선택):
  set GSND_MARINER_TEST=1
  c:/dev/gsnd_rag_v4/backend/.venv/Scripts/python.exe -m pytest tests/test_queryset_gsnd.py -q
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pytest

import app.mariner.queryset_gsnd as queryset_gsnd
from app.core.config import Config


@dataclass
class _FakeSelectSet:
    field_name: str

    def __init__(self, field_name: str, _num: int, _zero: int):
        self.field_name = str(field_name)


class _FakeWhereSet:
    def __init__(self, *args: Any):
        self.args = args


class _FakeFilterSet:
    def __init__(self, _op: Any, field: str, values: List[str], _weight: int):
        self.field = field
        self.values = list(values)


class _FakeOrderBySet:
    def __init__(self, *args: Any):
        self.args = args


class _FakeQuery:
    def __init__(self, _a: str, _b: str):
        self.selected_names: List[str] = []
        self.filter_set: Optional[List[_FakeFilterSet]] = None
        self.where_set_array: Optional[List[_FakeWhereSet]] = None

    def setResult(self, _start: int, _end: int):
        return None

    def setFrom(self, _collection: str):
        return None

    def setSearchKeyword(self, _keyword: str):
        return None

    def setSearch(self, _v: bool):
        return None

    def setDebug(self, _v: bool):
        return None

    def setPrintQuery(self, _v: bool):
        return None

    def setLoggable(self, _v: bool):
        return None

    def setFaultless(self, _v: bool):
        return None

    def setValue(self, _k: str, _v: str):
        return None

    def setSelect(self, select_set_array: List[_FakeSelectSet]):
        self.selected_names = [s.field_name for s in select_set_array]

    def setOrderby(self, _order_set_array: List[_FakeOrderBySet]):
        return None

    def setWhere(self, _where_set_array: List[_FakeWhereSet]):
        self.where_set_array = _where_set_array

    def setFilter(self, filter_set_array: List[_FakeFilterSet]):
        self.filter_set = filter_set_array


class _FakeQuerySet:
    def __init__(self, _size: int):
        self.query: Optional[_FakeQuery] = None

    def addQuery(self, query: _FakeQuery):
        self.query = query


class _FakeResult:
    def __init__(self, rows: List[Dict[str, Any]], query: _FakeQuery):
        self._rows = rows
        self._query = query

    def getRealSize(self) -> int:
        return len(self._rows)

    def getResult(self, i: int, idx: int) -> Any:
        field_name = self._query.selected_names[idx]
        return self._rows[i].get(field_name, "")


class _FakeResultSet:
    def __init__(self, rows: List[Dict[str, Any]], query: _FakeQuery):
        self._rows = rows
        self._query = query

    def getResult(self, _idx: int) -> _FakeResult:
        return _FakeResult(self._rows, self._query)


class _FakeCommandSearchRequest:
    last_instance: Optional["_FakeCommandSearchRequest"] = None

    def __init__(self, _ip: str, _port: int, rows: Optional[List[Dict[str, Any]]] = None):
        self._rows = rows or []
        self._queryset: Optional[_FakeQuerySet] = None
        _FakeCommandSearchRequest.last_instance = self

    def setProps(self, *_args: Any):
        return None

    def request(self, queryset: _FakeQuerySet) -> int:
        self._queryset = queryset
        return 0

    def getResultSet(self) -> _FakeResultSet:
        assert self._queryset is not None and self._queryset.query is not None
        return _FakeResultSet(self._rows, self._queryset.query)


def _install_fake_mariner(monkeypatch: pytest.MonkeyPatch, rows: List[Dict[str, Any]]) -> None:
    def _command_ctor(ip: str, port: int):
        return _FakeCommandSearchRequest(ip, port, rows=rows)

    class _FakeCommandPkg:
        CommandSearchRequest = staticmethod(_command_ctor)

    class _FakeQueryPkg:
        Query = _FakeQuery
        QuerySet = _FakeQuerySet
        SelectSet = _FakeSelectSet
        OrderBySet = _FakeOrderBySet
        WhereSet = _FakeWhereSet
        FilterSet = _FakeFilterSet

    def _jpackage(path: str):
        if path == "com.diquest.ir5.client.command":
            return _FakeCommandPkg
        if path == "com.diquest.ir5.common.msg.protocol.query":
            return _FakeQueryPkg
        raise AssertionError(f"unexpected JPackage path: {path}")

    monkeypatch.setattr(queryset_gsnd, "ensure_jvm_thread", lambda: None)
    monkeypatch.setattr(queryset_gsnd.jpype, "JPackage", _jpackage)
    monkeypatch.setattr(queryset_gsnd.jpype, "JArray", lambda _t: (lambda arr: arr))
    monkeypatch.setattr(queryset_gsnd.jpype, "JByte", int)
    monkeypatch.setattr(queryset_gsnd, "JString", lambda s: s)


def test_gsnd_applies_compli_dt_filter_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_mariner(monkeypatch, rows=[])

    queryset_gsnd.query_GSND_general_documents(
        "2026년 기초연금",
        collection=Config.RAG_COLLECTION,
        year_filters=["2026"],
        apply_year_filter=True,
    )

    cmd = _FakeCommandSearchRequest.last_instance
    assert cmd is not None
    assert cmd._queryset is not None and cmd._queryset.query is not None
    fset = cmd._queryset.query.filter_set
    assert fset is not None and len(fset) == 1
    assert fset[0].field == "COMPLI_DT"
    assert fset[0].values == ["20260101", "20261231"]


def test_gsnd_skips_compli_dt_filter_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_mariner(monkeypatch, rows=[])

    docs = queryset_gsnd.query_GSND_general_documents(
        "2026년 기초연금",
        collection=Config.RAG_COLLECTION,
        year_filters=["2026"],
        apply_year_filter=False,
    )
    print(f"[test_gsnd_skips_compli_dt_filter_when_disabled] fetched_docs={docs}")

    cmd = _FakeCommandSearchRequest.last_instance
    assert cmd is not None
    assert cmd._queryset is not None and cmd._queryset.query is not None
    assert cmd._queryset.query.filter_set is None


def test_gsnd_returns_rows_and_excludes_chunk_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = [
        {
            "CHUNK_ID": "277",
            "SIGUN": "창원시",
            "CHUNK_PATH": "a",
            "NAME": "기초연금",
            "COMPLI_DT": "20260110",
            "WEIGHT": "1000",
        },
        {
            "CHUNK_ID": "WLF00001164",
            "SIGUN": "창원시",
            "CHUNK_PATH": "b",
            "NAME": "장애인연금",
            "COMPLI_DT": "20260111",
            "WEIGHT": "1200",
        },
    ]
    _install_fake_mariner(monkeypatch, rows=rows)

    docs = queryset_gsnd.query_GSND_general_documents(
        "창원시 연금",
        collection=Config.RAG_COLLECTION,
        excluded_chunk_ids=["WLF00001164"],
        apply_year_filter=False,
    )

    assert len(docs) == 1
    assert docs[0]["CHUNK_ID"] == "277"
    assert docs[0]["NAME"] == "기초연금"


def test_gsnd_keyword_only_omits_vector_clause(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_mariner(monkeypatch, rows=[])

    queryset_gsnd.query_GSND_general_documents(
        "창원시 기초연금",
        collection=Config.RAG_COLLECTION,
        search_mode="keyword_only",
        apply_year_filter=False,
    )

    cmd = _FakeCommandSearchRequest.last_instance
    assert cmd is not None and cmd._queryset is not None and cmd._queryset.query is not None
    where_set = cmd._queryset.query.where_set_array or []
    where_args = [ws.args for ws in where_set]
    assert not any(args and args[0] == "TEXT_CHUNK_MI" for args in where_args)


def test_gsnd_hybrid_keeps_vector_clause(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_mariner(monkeypatch, rows=[])

    queryset_gsnd.query_GSND_general_documents(
        "창원시 기초연금",
        collection=Config.RAG_COLLECTION,
        search_mode="hybrid",
        apply_year_filter=False,
    )

    cmd = _FakeCommandSearchRequest.last_instance
    assert cmd is not None and cmd._queryset is not None and cmd._queryset.query is not None
    where_set = cmd._queryset.query.where_set_array or []
    where_args = [ws.args for ws in where_set]
    assert any(args and args[0] == "TEXT_CHUNK_MI" for args in where_args)


@pytest.mark.skipif(
    os.environ.get("GSND_MARINER_TEST", "").strip().lower() not in ("1", "true", "yes"),
    reason="실 Mariner 검색은 GSND_MARINER_TEST=1 일 때만 실행",
)
def test_gsnd_live_basic_search() -> None:
    if not Config.RAG_ENABLED:
        pytest.skip("RAG_ENABLED=False")

    docs = queryset_gsnd.query_GSND_general_documents(
        "2026년 기준 창원시 기초연금 신청 절차",
        collection=Config.RAG_COLLECTION,
        year_filters=["2026"],
        sigun_filters=["경상남도 창원시"],
    )

    print(f"[test_gsnd_live_basic_search] fetched_count={len(docs)}")
    for idx, doc in enumerate(docs[:3], start=1):
        print(
            "[test_gsnd_live_basic_search] "
            f"#{idx} CHUNK_ID={doc.get('CHUNK_ID', '')} "
            f"NAME={doc.get('NAME', '')} "
            f"SIGUN={doc.get('SIGUN', '')} "
            f"COMPLI_DT={doc.get('COMPLI_DT', '')}"
        )

    assert isinstance(docs, list)


@pytest.mark.skipif(
    os.environ.get("GSND_MARINER_TEST", "").strip().lower() not in ("1", "true", "yes"),
    reason="실 Mariner 검색은 GSND_MARINER_TEST=1 일 때만 실행",
)
def test_gsnd_live_includes_chunk_path_content() -> None:
    if not Config.RAG_ENABLED:
        pytest.skip("RAG_ENABLED=False")

    docs = queryset_gsnd.query_GSND_general_documents(
        "2026년 기준 창원시 기초연금 신청 절차",
        collection=Config.RAG_COLLECTION,
        year_filters=["2026"],
        sigun_filters=["경상남도 창원시"],
    )

    if not docs:
        pytest.skip("실검색 결과가 없어 CHUNK_PATH 본문 확인을 건너뜁니다.")

    non_empty_chunk_path_docs = [
        doc for doc in docs if str(doc.get("CHUNK_PATH", "") or "").strip()
    ]
    assert non_empty_chunk_path_docs, "CHUNK_PATH가 비어있는 문서만 반환되었습니다."

    sample_doc = non_empty_chunk_path_docs[0]
    sample_text = str(sample_doc.get("CHUNK_PATH", "") or "")
    sample_preview = sample_text.replace("\n", " ")[:200]
    print(f"[test_gsnd_live_includes_chunk_path_content] fetched_count={len(docs)}")
    print(
        "[test_gsnd_live_includes_chunk_path_content] "
        f"sample_chunk_id={sample_doc.get('CHUNK_ID', '')} "
        f"chunk_path_len={len(sample_text)} "
        f"chunk_path_preview={sample_preview}"
    )
