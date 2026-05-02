"""
Mariner 쿼리셋 — 추천 후속 질문(/recommended-question) 전용

메타데이터의 UI 문서명(display name)만으로 검색한다.
컬렉션은 오직 .env 의 다음 두 값만 사용한다 (다른 RAG_* 컬렉션 미사용).

- ``RAG_OKMS_COLLECTION`` (예: ``GSND_BIZ_DATASET_V4``) — 사업명 필드만 검색
- ``RAG_GOV_OKMS_COLLECTION`` (예: ``GOV_OKMS_V1``) — 서비스명 필드만 검색
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import jpype
from jpype import JString

from app.core.config import Config
from app.core.constants import (
    MARINER_SELECT_FIELD_NUM,
    MARINER_SETPROPS_EXTRA,
    OP_BRACE_OPEN,
    OP_OR,
    OP_BRACE_CLOSE,
    OP_AND,
    OP_NOT,
    OP_INT_SUMMATION,
    OP_HASANY,
    MARINER_WEIGHT_HIGH,
)
from app.core.exceptions import RAGServiceError
from app.mariner.jvm_manager import ensure_jvm_thread
from app.mariner.queryset_okms import _build_okms_document_name
from app.mariner.queryset_gov_okms import (
    _LIFECYCLE_MAP,
    _build_gov_okms_document_name,
    _map_lifecycle_for_gov_okms,
)

logger = logging.getLogger(__name__)

_LOG = "recommended_question_name"


def _biz_okms_collection() -> str:
    """지자체 OKMS 사업 데이터 — ``Config.RAG_OKMS_COLLECTION`` (.env ``RAG_OKMS_COLLECTION``)."""
    return (Config.RAG_OKMS_COLLECTION or "").strip()


def _gov_okms_collection() -> str:
    """행정안전부 GOV_OKMS — ``Config.RAG_GOV_OKMS_COLLECTION`` (.env ``RAG_GOV_OKMS_COLLECTION``)."""
    return (Config.RAG_GOV_OKMS_COLLECTION or "").strip()


def query_okms_documents_by_display_name(
    display_name: str,
    year_filters: Optional[List[str]] = None,
    sigun_filters: Optional[List[str]] = None,
    lifecycle_filter: Optional[str] = None,
    excluded_chunk_ids: Optional[List[str]] = None,
    max_results: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    지자체 OKMS 사업 컬렉션(``RAG_OKMS_COLLECTION``) — 문서명(사업명) 필드만 QuerySet(1) 검색.

    UI ``referenced_documents[].name``과 동일한 문자열을 검색어로 사용한다.
    (인덱스상 ORG_NM 전용 필드가 없어 사업명 필드 기준이다.)
    """
    t1 = time.monotonic()
    if not Config.RAG_ENABLED:
        logger.warning("[%s/OKMS] RAG 비활성화", _LOG)
        return []

    q = str(display_name or "").strip()
    if not q:
        return []

    collection = _biz_okms_collection()
    if not collection:
        logger.error("[%s/OKMS] RAG_OKMS_COLLECTION 미설정 (.env)", _LOG)
        return []

    max_top_n = int(max_results or Config.MARINER_MAX_RESULTS or 5)
    max_top_n = max(1, min(max_top_n, 20))

    excluded_chunk_set = {
        str(chunk_id).strip()
        for chunk_id in (excluded_chunk_ids or [])
        if str(chunk_id).strip()
    }
    sigun_scriptlet_values = [
        s for s in (sigun_filters or [])
        if s and s != "경상남도"
    ]

    try:
        ensure_jvm_thread()
        timeout = Config.MARINER_TIMEOUT
        threshold = Config.MARINER_THRESHOLD

        jpkg_cmd = jpype.JPackage("com.diquest.ir5.client.command")
        command = jpkg_cmd.CommandSearchRequest(Config.MARINER_IP, int(Config.MARINER_PORT))
        command.setProps(
            Config.MARINER_IP, int(Config.MARINER_PORT),
            timeout, MARINER_SETPROPS_EXTRA, MARINER_SETPROPS_EXTRA,
        )
        jpkg_query = jpype.JPackage("com.diquest.ir5.common.msg.protocol.query")

        num = MARINER_SELECT_FIELD_NUM
        select_field_names = [
            "ID", "YEAR", "SIGUN", "CONTENT", "WEIGHT", "LIFE_CYCLE",
            "DEPARTMENT", "APPLICATION_PERIOD", "PURPOSE", "TEL",
            "BUSINESS_NAME", "ORG_NM", "PATH",
        ]
        select_set_array = [
            jpkg_query.SelectSet(JString(field_name), num, 0)
            for field_name in select_field_names
        ]
        field_indexes = {field_name: idx for idx, field_name in enumerate(select_field_names)}

        query = jpkg_query.Query("", "")
        ks = JString(q)
        query.setResult(0, max_top_n - 1)
        query.setSearchKeyword(ks)
        query.setFrom(collection)
        query.setSearch(True)
        query.setDebug(True)
        query.setPrintQuery(True)
        query.setLoggable(True)
        query.setValue("VS_THRESHOLD", str(threshold))
        query.setValue("VS_RESULT_SIZE", str(max_top_n))
        query.setSelect(select_set_array)
        query.setOrderby([jpkg_query.OrderBySet(True, "WEIGHT", jpype.JByte(97))])

        where_set_array = [
            jpkg_query.WhereSet(OP_BRACE_OPEN),
            jpkg_query.WhereSet("BUSINESS_NAME_KO", OP_HASANY, ks, MARINER_WEIGHT_HIGH),
            jpkg_query.WhereSet(OP_OR),
            jpkg_query.WhereSet("BUSINESS_NAME_MI", OP_HASANY, ks, MARINER_WEIGHT_HIGH),
            jpkg_query.WhereSet(OP_OR),
        ]

        if sigun_scriptlet_values:
            if len(sigun_scriptlet_values) == 1:
                where_set_array += [
                    jpkg_query.WhereSet(OP_AND),
                    jpkg_query.WhereSet("SIGUN", OP_INT_SUMMATION, sigun_scriptlet_values[0], 0),
                ]
            else:
                where_set_array.append(jpkg_query.WhereSet(OP_AND))
                where_set_array.append(jpkg_query.WhereSet(OP_BRACE_OPEN))
                for idx, sv in enumerate(sigun_scriptlet_values):
                    if idx > 0:
                        where_set_array.append(jpkg_query.WhereSet(OP_OR))
                    where_set_array.append(jpkg_query.WhereSet("SIGUN", OP_INT_SUMMATION, sv, 0))
                where_set_array.append(jpkg_query.WhereSet(OP_BRACE_CLOSE))

        if lifecycle_filter:
            where_set_array += [
                jpkg_query.WhereSet(OP_AND),
                jpkg_query.WhereSet("LIFE_CYCLE", 34, lifecycle_filter, 0),
            ]

        if excluded_chunk_set:
            for chunk_id in sorted(excluded_chunk_set):
                where_set_array += [
                    jpkg_query.WhereSet(OP_NOT),
                    jpkg_query.WhereSet("ID", OP_INT_SUMMATION, chunk_id, 0),
                ]

        query.setWhere(where_set_array)

        if year_filters:
            filter_years = sorted(set(str(y).strip() for y in year_filters if str(y).strip()))
            if filter_years:
                min_year, max_year = filter_years[0], filter_years[-1]
                query.setFilter([
                    jpkg_query.FilterSet(
                        jpype.JByte(3), "YEAR",
                        jpype.JArray(jpype.JString)([f"{min_year}0101", f"{max_year}1231"]), 0,
                    ),
                ])

        queryset = jpkg_query.QuerySet(1)
        queryset.addQuery(query)

        logger.info("[%s/OKMS] name-only 검색: %r collection=%s", _LOG, q[:80], collection)
        ret = command.request(queryset)
        if ret < 0:
            logger.error("[%s/OKMS] 요청 오류: %s", _LOG, ret)
            raise RAGServiceError(f"Mariner API 반환 코드: {ret}")

        result_set = command.getResultSet()
        if result_set is None:
            logger.warning("[%s/OKMS] 결과 없음", _LOG)
            return []

        result = result_set.getResult(0)
        result_size = result.getRealSize()
        docs: List[Dict[str, Any]] = []
        for i in range(result_size):
            try:
                raw_weight = result.getResult(i, field_indexes["WEIGHT"])
                weight_val = float(str(raw_weight)) * 0.0001
            except (ValueError, TypeError):
                weight_val = 0.0

            if excluded_chunk_set:
                rid = str(result.getResult(i, field_indexes["ID"]) or "").strip()
                if rid and rid in excluded_chunk_set:
                    continue

            doc = {field_name: str(result.getResult(i, idx) or "") for field_name, idx in field_indexes.items()}
            doc["WEIGHT"] = str(weight_val)
            doc["CHUNK_ID"] = doc.get("ID", "")
            doc["NAME"] = _build_okms_document_name(doc)
            doc["CHUNK_PATH"] = str(doc.get("CONTENT", "") or "")

            docs.append(doc)

        logger.info(
            "[%s/OKMS] %.3fs 결과 %d건 (name=%r)",
            _LOG, time.monotonic() - t1, len(docs), q[:60],
        )
        return docs

    except RAGServiceError:
        raise
    except Exception as e:
        logger.error("[%s/OKMS] 오류: %s", _LOG, e, exc_info=True)
        raise RAGServiceError(f"OKMS 문서명 검색 오류: {str(e)}") from e


def query_gov_okms_documents_by_display_name(
    display_name: str,
    lifecycle_filter: Optional[str] = None,
    sigun_filters: Optional[List[str]] = None,
    excluded_chunk_ids: Optional[List[str]] = None,
    max_results: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """행정안전부 GOV 컬렉션(``RAG_GOV_OKMS_COLLECTION``) — 서비스명 필드만 QuerySet(1) 검색."""
    t1 = time.monotonic()
    if not Config.RAG_ENABLED:
        logger.warning("[%s/GOV] RAG 비활성화", _LOG)
        return []

    q = str(display_name or "").strip()
    if not q:
        return []

    collection = _gov_okms_collection()
    if not collection:
        logger.error("[%s/GOV] RAG_GOV_OKMS_COLLECTION 미설정 (.env)", _LOG)
        return []

    mapped_lifecycle = _map_lifecycle_for_gov_okms(lifecycle_filter)
    sigun_scriptlet_values = [s for s in (sigun_filters or []) if s and s != "경상남도"]

    excluded_chunk_set = {
        str(chunk_id).strip()
        for chunk_id in (excluded_chunk_ids or [])
        if str(chunk_id).strip()
    }

    _top_n = int(max_results or 8)
    _top_n = max(1, min(_top_n, 20))
    _threshold = 0.2
    _result_size = 50

    try:
        ensure_jvm_thread()
        jpkg_cmd = jpype.JPackage("com.diquest.ir5.client.command")
        command = jpkg_cmd.CommandSearchRequest(Config.MARINER_IP, int(Config.MARINER_PORT))
        command.setProps(Config.MARINER_IP, int(Config.MARINER_PORT), Config.MARINER_TIMEOUT, 100, 100)
        jpkg_query = jpype.JPackage("com.diquest.ir5.common.msg.protocol.query")

        num = 16
        select_field_names = [
            "SERVICE_ID", "SERVICE_NAME", "RESPONSIBLE_MINISTRY", "TARGET_DETAILS",
            "SELECTION_CRITERIA", "BENEFIT_DETAILS", "CONTACT_POINT", "SERVICE_TYPE",
            "LIFE_CYCLE", "INTEREST_TOPICS", "WEIGHT", "SERVICE_DESCRIPTION",
            "APPLICATION_PERIOD",
        ]
        select_set_array = [jpkg_query.SelectSet(JString(f), num, 0) for f in select_field_names]
        field_indexes = {f: i for i, f in enumerate(select_field_names)}

        query = jpkg_query.Query("", "")
        ks = JString(q)
        query.setResult(0, _top_n - 1)
        query.setFrom(collection)
        query.setSearch(True)
        query.setDebug(True)
        query.setPrintQuery(True)
        query.setLoggable(True)
        query.setValue("VS_THRESHOLD", str(_threshold))
        query.setValue("VS_RESULT_SIZE", str(_result_size))
        query.setSelect(select_set_array)
        query.setOrderby([jpkg_query.OrderBySet(True, "WEIGHT", jpype.JByte(97))])

        where_set_array = [
            jpkg_query.WhereSet(OP_BRACE_OPEN),
            jpkg_query.WhereSet("SERVICE_NAME_KO", 2, ks, 0.7),
            jpkg_query.WhereSet(OP_OR),
            jpkg_query.WhereSet("SERVICE_NAME_MI", 2, ks, 0.3),
            jpkg_query.WhereSet(OP_BRACE_CLOSE),
        ]

        if mapped_lifecycle:
            where_set_array += [
                jpkg_query.WhereSet(OP_AND),
                jpkg_query.WhereSet("LIFE_CYCLE", 34, mapped_lifecycle, 0),
            ]

        if excluded_chunk_set:
            for chunk_id in sorted(excluded_chunk_set):
                where_set_array += [
                    jpkg_query.WhereSet(OP_NOT),
                    jpkg_query.WhereSet("SERVICE_ID", OP_INT_SUMMATION, chunk_id, 0),
                ]

        query.setWhere(where_set_array)
        queryset = jpkg_query.QuerySet(1)
        queryset.addQuery(query)

        logger.info("[%s/GOV] name-only 검색: %r collection=%s", _LOG, q[:80], collection)
        ret = command.request(queryset)
        if ret < 0:
            logger.error("[%s/GOV] 요청 오류: %s", _LOG, ret)
            raise RAGServiceError(f"Mariner API 반환 코드: {ret}")

        result_set = command.getResultSet()
        if result_set is None:
            return []

        result = result_set.getResult(0)
        result_size = result.getRealSize()
        docs: List[Dict[str, Any]] = []
        for i in range(result_size):
            try:
                raw_weight = result.getResult(i, field_indexes["WEIGHT"])
                weight_val = float(str(raw_weight)) * 0.0001
            except (ValueError, TypeError):
                weight_val = 0.0

            doc = {f: str(result.getResult(i, idx) or "") for f, idx in field_indexes.items()}
            doc["WEIGHT"] = str(weight_val)
            doc["CHUNK_ID"] = doc.get("SERVICE_ID", "")
            if excluded_chunk_set and doc["CHUNK_ID"] in excluded_chunk_set:
                continue

            doc["ID"] = doc["CHUNK_ID"]
            doc["NAME"] = _build_gov_okms_document_name(doc)
            doc["BUSINESS_NAME"] = doc.get("SERVICE_NAME", "")
            doc["ORG_NM"] = doc.get("RESPONSIBLE_MINISTRY", "")
            _content_parts = []
            if doc.get("SERVICE_DESCRIPTION", "").strip():
                _content_parts.append(doc["SERVICE_DESCRIPTION"].strip())
            for _field, _label in [
                ("TARGET_DETAILS", "지원대상"),
                ("BENEFIT_DETAILS", "지원내용"),
                ("CONTACT_POINT", "문의처"),
                ("LIFE_CYCLE", "생애주기"),
            ]:
                _val = doc.get(_field, "").strip()
                if _val:
                    if _field == "LIFE_CYCLE":
                        _val = {v: k for k, v in _LIFECYCLE_MAP.items()}.get(_val, _val)
                    _content_parts.append(f"{_label}: {_val}")
            _combined = "\n".join(_content_parts)
            doc["CONTENT"] = _combined
            doc["CHUNK_PATH"] = _combined
            doc.setdefault("SIGUN", sigun_scriptlet_values[0] if sigun_scriptlet_values else "")
            doc.setdefault("YEAR", "")
            docs.append(doc)

        logger.info(
            "[%s/GOV] %.3fs 결과 %d건 (name=%r)",
            _LOG, time.monotonic() - t1, len(docs), q[:60],
        )
        return docs

    except RAGServiceError:
        raise
    except Exception as e:
        logger.error("[%s/GOV] 오류: %s", _LOG, e, exc_info=True)
        raise RAGServiceError(f"GOV_OKMS 문서명 검색 오류: {str(e)}") from e
