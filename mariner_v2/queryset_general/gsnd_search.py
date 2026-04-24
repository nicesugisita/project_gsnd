"""GSND 보강 검색 전용 Mariner 검색"""

import logging
import re
import time
from typing import Dict, Any, List, Optional

import jpype
from jpype import JString

from core.config import Config
from core.constants import (
    MARINER_SELECT_FIELD_NUM,
    MARINER_SETPROPS_EXTRA,
    MARINER_WS_OR,
    MARINER_WS_AND,
    MARINER_WS_END,
    MARINER_WS_FILTER,
    MARINER_WS_BM25, MARINER_WS_VECTOR,
    MARINER_WEIGHT_HIGH,
    MARINER_WEIGHT_MED,
)
from core.exceptions import RAGServiceError
from mariner.queryset import normalize_sigun
from mariner_v2.jvm_manager import ensure_jvm_thread
from .schema_helpers import (
    _uses_okms_document_schema,
    _uses_gsnd_v7_schema,
    _uses_welfare_center_schema,
    _build_okms_document_name,
    _LIFECYCLE_CONTENT_KEYWORDS,
)

logger = logging.getLogger(__name__)


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

        startnum = 0
        endnum = int(max_top_n) - 1
        query.setResult(startnum, endnum)
        query.setFrom(collection)
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
        keyword_string = JString(keyword)
        where_set_array = [
            jpkg_query.WhereSet(MARINER_WS_OR),                                        # OR (
            jpkg_query.WhereSet("NAME_KO",        2,  keyword_string, MARINER_WEIGHT_HIGH),
            jpkg_query.WhereSet(MARINER_WS_AND),                                        #   OR
            jpkg_query.WhereSet("TEXT_CHUNK_KO", MARINER_WS_BM25,  keyword_string, MARINER_WEIGHT_HIGH),
            jpkg_query.WhereSet(MARINER_WS_AND),                                        #   OR
            jpkg_query.WhereSet("NAME_MI",        2,  keyword_string, MARINER_WEIGHT_HIGH),
            jpkg_query.WhereSet(MARINER_WS_AND),                                        #   OR
            jpkg_query.WhereSet("TEXT_CHUNK_MI", MARINER_WS_VECTOR, keyword_string, MARINER_WEIGHT_MED),
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

        # TODO: COMPLI_DT 가중치 부스트 및 FilterSet — year_filters 전달 시 적용 예정
        # 상세 계획: wip/TODO_GSND_COMPLI_DT.md

        # YEAR FilterSet (연도 필터가 있는 경우)
        if year_filters:
            year_ranges = []
            for y in year_filters:
                y = str(y).strip()
                if y:
                    year_ranges.extend([f"{y}0101", f"{y}1231"])
            if year_ranges:
                filter_set_array = [
                    jpkg_query.FilterSet(
                        jpype.JByte(3), "COMPLI_DT",
                        jpype.JArray(jpype.JString)(year_ranges), 0
                    )
                ]

        else:
            filter_set_array = [jpkg_query.FilterSet(jpype.JByte(3), "COMPLI_DT", jpype.JArray(jpype.JString)(["20260101", "20261231"]), 0)]
        query.setFilter(filter_set_array)

        query.setWhere(where_set_array)

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
