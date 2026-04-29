"""
Mariner 쿼리셋 — GSND_DATASET_V8 전용

GSND 보강 검색(general Step 7-B)에 사용되는 Mariner 쿼리 함수입니다.

기존 mariner/queryset.py, services/rag_service.py는 변경하지 않습니다.
"""

import logging
import re
import time
from datetime import date
from typing import Dict, Any, List, Optional

import jpype
from jpype import JString

from app.core.config import Config
from app.core.constants import (
    MARINER_SELECT_FIELD_NUM,
    MARINER_SETPROPS_EXTRA,
    MARINER_WS_OR,
    MARINER_WS_AND,
    MARINER_WS_END,
    MARINER_WS_FILTER,
    MARINER_WS_NOT,
    MARINER_WS_EXACT,
    MARINER_WS_BM25, MARINER_WS_VECTOR,
    MARINER_WEIGHT_HIGH,
    MARINER_WEIGHT_MED,
)
from app.core.exceptions import RAGServiceError
from app.mariner.sigun_utils import normalize_sigun
from app.mariner.jvm_manager import ensure_jvm_thread

logger = logging.getLogger(__name__)


def _uses_okms_document_schema(collection: Optional[str]) -> bool:
    return (collection or "").strip().upper() == Config.RAG_OKMS_COLLECTION.upper()


def _uses_gsnd_v7_schema(collection: Optional[str]) -> bool:
    return (collection or "").strip().upper() == Config.RAG_COLLECTION.upper()


def _uses_welfare_center_schema(collection: Optional[str]) -> bool:
    return (collection or "").strip().upper() == Config.RAG_WELFARE_CENTER_COLLECTION.upper()


def _build_okms_document_name(doc: Dict[str, Any]) -> str:
    org_nm = str(doc.get("ORG_NM", "") or "").strip()
    if org_nm:
        return org_nm
    business_name = str(doc.get("BUSINESS_NAME", "") or "").strip()
    if business_name:
        return business_name
    sigun = str(doc.get("SIGUN", "") or "").strip()
    year = str(doc.get("YEAR", "") or "").strip()
    parts = []
    if sigun:
        parts.append(sigun)
    if year:
        parts.append(year)
    return " / ".join(parts) or str(doc.get("CHUNK_ID", "") or "문서")


_LIFECYCLE_CONTENT_KEYWORDS: Dict[str, List[str]] = {
    "영유아": ["영유아"],
    "아동": ["아동"],
    "청소년": ["청소년"],
    "청년": ["청년"],
    "중장년": ["중장년"],
    "노인": ["노인", "노년"],
}


# ============================================================
# GSND_DATASET_V8 보강 검색
# ============================================================

def query_GSND_general_documents(
    keyword: str,
    collection: str = None,
    user_id: Optional[str] = None,
    conv_id: Optional[str] = None,
    year_filters: Optional[List[str]] = None,
    sigun_filters: Optional[List[str]] = None,
    lifecycle_filter: Optional[str] = None,
    facility_type_filter: Optional[str] = None,
    search_mode: str = "hybrid",
    excluded_chunk_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    general 플로우의 GSND 보강 검색 (Step 7-B)에서 사용하는 Mariner 검색 함수.
    GSND 보강 검색 조건 변경 시 이 함수를 수정합니다.
    검색 결과 수는 Config.MARINER_MAX_RESULTS (.env MARINER_MAX_RESULTS) 로 제어합니다.
    """
    t1 = time.monotonic()

    if not Config.RAG_ENABLED:
        logger.warning("RAG가 비활성화되어 있습니다.")
        return []

    if collection is None:
        collection = Config.RAG_COLLECTION

    excluded_chunk_set = {
        str(chunk_id).strip()
        for chunk_id in (excluded_chunk_ids or [])
        if str(chunk_id).strip()
    }
    if excluded_chunk_set:
        excluded_values = sorted(excluded_chunk_set)
        logger.info(
            "[MoreResults][Mariner/general/GSND] 제외 입력 수=%d | 샘플=%s",
            len(excluded_values),
            excluded_values[:10],
        )

    try:
        timeout = Config.MARINER_TIMEOUT
        threshold = Config.MARINER_THRESHOLD
        max_top_n = Config.MARINER_MAX_RESULTS

        ensure_jvm_thread()

        jpkg_cmd = jpype.JPackage("com.diquest.ir5.client.command")
        command = jpkg_cmd.CommandSearchRequest(Config.MARINER_IP, int(Config.MARINER_PORT))
        command.setProps(Config.MARINER_IP, int(Config.MARINER_PORT), timeout, MARINER_SETPROPS_EXTRA, MARINER_SETPROPS_EXTRA)

        jpkg_query = jpype.JPackage("com.diquest.ir5.common.msg.protocol.query")
        query = jpkg_query.Query("", "")
        print("----------------------------------------------",keyword)
        keyword_string = JString(keyword)
        print("----------------------------------------------",keyword_string)

        startnum = 0
        endnum = int(max_top_n) - 1
        query.setResult(startnum, endnum)
        query.setFrom(collection)
        query.setSearchKeyword(keyword_string)
        query.setSearch(True)
        query.setDebug(True)
        query.setPrintQuery(True)
        query.setLoggable(True)

        query.setValue("VS_THRESHOLD", str(threshold))
        query.setValue("VS_RESULT_SIZE", str(max_top_n))

        # SELECT 필드 (GSND_DATASET_V8 전용)
        num = MARINER_SELECT_FIELD_NUM
        select_field_names = ["CHUNK_ID", "SIGUN", "CHUNK_PATH", "NAME", "COMPLI_DT", "WEIGHT"]

        normalized_user_id = str(user_id or "").strip()
        normalized_conv_id = str(conv_id or "").strip()
        apply_upload_filter = bool(normalized_user_id and normalized_conv_id)

        if apply_upload_filter:
            select_field_names.extend(["USER_ID", "CONV_ID"])

        select_set_array = [
            jpkg_query.SelectSet(JString(field_name), num, 0)
            for field_name in select_field_names
        ]
        field_indexes = {field_name: idx for idx, field_name in enumerate(select_field_names)}
        query.setSelect(select_set_array)

        # 정렬: 가중치 우선 → 동점 시 COMPLI_DT 정렬 (JByte(97))
        order_set_array = [jpkg_query.OrderBySet(True, "COMPLI_DT", jpype.JByte(97))]
        query.setOrderby(order_set_array)

        # WHERE: 4-field OR 검색식
        where_set_array = [
            jpkg_query.WhereSet(MARINER_WS_OR),                                        # OR (
            jpkg_query.WhereSet("NAME_KO",        2,  keyword_string, MARINER_WEIGHT_MED),
            jpkg_query.WhereSet(MARINER_WS_AND),                                        #   OR
            jpkg_query.WhereSet("TEXT_CHUNK_KO", MARINER_WS_BM25,  keyword_string, MARINER_WEIGHT_MED),
            jpkg_query.WhereSet(MARINER_WS_AND),                                        #   OR
            jpkg_query.WhereSet("NAME_MI",        2,  keyword_string, MARINER_WEIGHT_HIGH),
            jpkg_query.WhereSet(MARINER_WS_AND),                                        #   OR
            jpkg_query.WhereSet("TEXT_CHUNK_MI", MARINER_WS_VECTOR, keyword_string, MARINER_WEIGHT_HIGH),
            jpkg_query.WhereSet(MARINER_WS_END),                                       # )
        ]

        # SIGUN 스크립틀릿 (op 1)
        if sigun_filters:
            short_siguns = []
            skip_scriptlet = False
            for s in sigun_filters:
                s = str(s).strip()
                if s == "경상남도":
                    skip_scriptlet = True
                    break
                short_siguns.append(s)
            if short_siguns and not skip_scriptlet:
                sigun_str = JString(" ".join(short_siguns))
                where_set_array += [
                    jpkg_query.WhereSet(MARINER_WS_FILTER),
                    jpkg_query.WhereSet("SIGUN", 1, sigun_str, 0),
                ]

        # CHUNK_ID 제외 필터 (예제 패턴: NOT + EXACT 반복)
        if excluded_chunk_set:
            excluded_values = sorted(excluded_chunk_set)
            # 컬렉션 스키마별 식별 필드 결정
            if _uses_okms_document_schema(collection) or _uses_welfare_center_schema(collection):
                id_field = "ID"
            else:
                id_field = "CHUNK_ID"

            _n = len(excluded_values)
            _sample = excluded_values[:20]
            logger.info(
                "[MoreResults][Mariner/general/GSND] 검색단 제외 IDs(%d) field=%s: %s%s",
                _n,
                id_field,
                _sample,
                "..." if _n > 20 else "",
            )
            for chunk_id in excluded_values:
                where_set_array += [
                    jpkg_query.WhereSet(MARINER_WS_NOT),
                    jpkg_query.WhereSet(id_field, MARINER_WS_EXACT, chunk_id, 0),
                ]

        query.setWhere(where_set_array)

        # COMPLI_DT FilterSet (감지된 연도의 최소~최대 범위, 없으면 올해)
        if year_filters:
            filter_years = sorted(set(str(y).strip() for y in year_filters if str(y).strip()))
        else:
            filter_years = [str(date.today().year)]

        if filter_years:
            min_year = filter_years[0]
            max_year = filter_years[-1]
            filter_set_array = [
                jpkg_query.FilterSet(
                    jpype.JByte(3), "COMPLI_DT",
                    jpype.JArray(jpype.JString)([f"{min_year}0101", f"{max_year}1231"]), 0
                )
            ]
            query.setFilter(filter_set_array)
            logger.debug(f"[Mariner/general/GSND] COMPLI_DT FilterSet: {min_year}~{max_year}")

        queryset = jpkg_query.QuerySet(1)
        queryset.addQuery(query)
        ret = command.request(queryset)

        if ret < 0:
            logger.error(f"[Mariner/general/GSND] 요청 오류: {ret}")
            raise RAGServiceError(f"Mariner API 반환 코드: {ret}")

        resultSet = command.getResultSet()
        if resultSet is None:
            logger.warning("[Mariner/general/GSND] 서버에서 결과를 받지 못했습니다.")
            return []

        result = resultSet.getResult(0)
        result_size = result.getRealSize()
        doc_list = []
        target_years = {
            str(year).strip()
            for year in (year_filters or [])
            if str(year).strip()
        }
        target_siguns = {
            normalize_sigun(str(sigun).strip())
            for sigun in (sigun_filters or [])
            if str(sigun).strip()
        }
        logger.info(f"[Mariner/general/GSND] raw 결과: {result_size}개 (필터 전), 키워드: {keyword[:50]}, sigun_filters={list(target_siguns) if target_siguns else None}")
        raw_id_samples: List[str] = []

        for i in range(result_size):
            try:
                raw_weight = result.getResult(i, field_indexes["WEIGHT"])
                weight_val = float(str(raw_weight)) * 0.0001
            except (ValueError, TypeError):
                weight_val = 0.0

            # if weight_val < 0.5:
            #     continue

            if apply_upload_filter:
                result_user_id = str(result.getResult(i, field_indexes["USER_ID"]) or "").strip()
                result_conv_id = str(result.getResult(i, field_indexes["CONV_ID"]) or "").strip()
                if result_user_id != normalized_user_id or result_conv_id != normalized_conv_id:
                    continue

            doc = {field_name: str(result.getResult(i, idx) or "") for field_name, idx in field_indexes.items()}
            doc["WEIGHT"] = str(weight_val)
            if doc.get("CHUNK_ID") and len(raw_id_samples) < 10:
                raw_id_samples.append(str(doc.get("CHUNK_ID")))

            if _uses_okms_document_schema(collection):
                doc["CHUNK_ID"] = doc.get("ID", "")
                doc["NAME"] = _build_okms_document_name(doc)
                doc["CHUNK_PATH"] = str(doc.get("CONTENT", "") or "")

                if target_years:
                    doc_year = str(doc.get("YEAR", "") or "")
                    matched_year = re.search(r"(?:19|20)\d{2}", doc_year)
                    normalized_doc_year = matched_year.group(0) if matched_year else doc_year.strip()
                    if normalized_doc_year not in target_years:
                        continue

                if target_siguns:
                    doc_sigun = str(doc.get("SIGUN", "") or "").strip()
                    if doc_sigun not in target_siguns:
                        continue

                if lifecycle_filter:
                    doc_lifecycle = str(doc.get("LIFE_CYCLE", "") or "").strip()
                    check_terms = _LIFECYCLE_CONTENT_KEYWORDS.get(lifecycle_filter, [lifecycle_filter])
                    if doc_lifecycle not in check_terms:
                        continue

            elif _uses_gsnd_v7_schema(collection):
                if target_siguns:
                    doc_sigun = str(doc.get("SIGUN", "") or "").strip()
                    if doc_sigun not in target_siguns:
                        continue

            elif _uses_welfare_center_schema(collection):
                doc["CHUNK_ID"] = doc.get("ID", "")
                doc["NAME"] = str(doc.get("FACILITY_NAME", "") or "").strip()
                doc["CHUNK_PATH"] = str(doc.get("ADDRESS", "") or "").strip()

                if target_siguns:
                    doc_sigun = str(doc.get("SIGUN", "") or "").strip()
                    doc_address = str(doc.get("ADDRESS", "") or "").strip()
                    sigun_matched = False
                    for ts in target_siguns:
                        if ts == "경상남도":
                            sigun_matched = True
                            break
                        short = ts.split(" ", 1)[1] if " " in ts else ts
                        if doc_sigun == short or short in doc_address:
                            sigun_matched = True
                            break
                    if not sigun_matched:
                        continue

                if facility_type_filter:
                    doc_facility_type = str(doc.get("FACILITY_TYPE", "") or "").strip()
                    if doc_facility_type != facility_type_filter:
                        continue

            doc_list.append(doc)

        if excluded_chunk_set:
            before_count = len(doc_list)
            removed_ids: List[str] = []
            filtered_docs: List[Dict[str, Any]] = []
            for doc in doc_list:
                cid = str(doc.get("CHUNK_ID", "")).strip()
                if cid and cid in excluded_chunk_set:
                    if len(removed_ids) < 20:
                        removed_ids.append(cid)
                    continue
                filtered_docs.append(doc)
            doc_list = filtered_docs
            logger.info(
                f"[MoreResults][Mariner/general/GSND] CHUNK_ID 1차 제외: {before_count - len(doc_list)}개"
            )
            logger.info(
                "[MoreResults][Mariner/general/GSND] 실제 제외 ID 샘플=%s | raw ID 샘플=%s",
                removed_ids[:10],
                raw_id_samples,
            )

        t2 = time.monotonic()
        logger.info(f"[Mariner/general/GSND] 검색 시간: {t2 - t1:.3f}초, 키워드: {keyword[:50]}, 결과: {len(doc_list)}개")
        for i, doc in enumerate(doc_list, 1):
            logger.debug(f"[Mariner/general/GSND] #{i} CHUNK_ID={doc.get('CHUNK_ID', '?')}, NAME={doc.get('NAME', '?')}, WEIGHT={doc.get('WEIGHT', '?')}")

        return doc_list

    except RAGServiceError:
        raise
    except Exception as e:
        logger.error(f"[Mariner/general/GSND] 예상치 못한 오류: {e}", exc_info=True)
        raise RAGServiceError(f"문서 검색 중 오류: {str(e)}") from e
