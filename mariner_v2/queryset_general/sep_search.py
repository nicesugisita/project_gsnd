"""SEP(OKMS Group A/B) 전용 Mariner 검색"""

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
    MARINER_WS_BM25,
    MARINER_WS_EXACT,
    MARINER_WEIGHT_HIGH,
    MARINER_WEIGHT_MED,
    MARINER_WEIGHT_LOW,
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


def query_SEP_general_documents(
    vector: str,
    keyword: str,
    collection: str = None,
    user_id: Optional[str] = None,
    conv_id: Optional[str] = None,
    year_filters: Optional[List[str]] = None,
    sigun_filters: Optional[List[str]] = None,
    lifecycle_filter: Optional[str] = None,
    facility_type_filter: Optional[str] = None,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    general 플로우의 OKMS 검색 (Group A/B)에서 사용하는 Mariner 검색 함수.
    SEP(Search Engine Process) 전용으로, OKMS 검색 조건 변경 시 이 함수를 수정합니다.
    검색 결과 수는 Config.MARINER_MAX_RESULTS (.env MARINER_MAX_RESULTS) 로 제어합니다.

    Returns:
        (keyword_docs, vector_docs) 튜플
    """
    t1 = time.monotonic()

    if not Config.RAG_ENABLED:
        logger.warning("RAG가 비활성화되어 있습니다.")
        return [], []

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
        queryset = jpkg_query.QuerySet(2)

        # 공통 설정 (루프 밖에서 1회만 계산)
        num = MARINER_SELECT_FIELD_NUM
        if _uses_okms_document_schema(collection):
            select_field_names = ["ID", "YEAR", "SIGUN", "CONTENT", "WEIGHT", "LIFE_CYCLE", "DEPARTMENT", "APPLICATION_PERIOD", "PURPOSE", "TEL", "BUSINESS_NAME", "ORG_NM", "PATH"]
        elif _uses_gsnd_v7_schema(collection):
            select_field_names = ["CHUNK_ID", "SIGUN", "CHUNK_PATH", "NAME", "DATE", "WEIGHT", "LABEL"]
        elif _uses_welfare_center_schema(collection):
            select_field_names = ["ID", "SIGUN", "FACILITY_NAME", "ADDRESS", "FACILITY_TYPE", "FACILITY_CATEGORY", "FACILITY_CAPACITY", "HOMEPAGE", "TEL", "WEIGHT"]
        else:
            select_field_names = ["CHUNK_ID", "NAME", "CHUNK_PATH", "WEIGHT"]

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

        # sigun 존재 여부에 따라 BUSINESS_NAME_MI 가중치 변경
        has_sigun = bool(sigun_filters and any(s != "경상남도" for s in sigun_filters))
        biz_name_mi_weight = 2.0 if has_sigun else MARINER_WEIGHT_HIGH

        # sigun 스크립틀릿용 값 (첫 번째 시군 필터 사용)
        sigun_scriptlet = ""
        if sigun_filters:
            sigun_scriptlet = sigun_filters[0]

        # Query[0]: keyword(트리플쿼리) 사용, Query[1]: vector(확장쿼리) 사용
        search_strings = [JString(keyword), JString(vector)]

        for i in range(2):
            query = jpkg_query.Query("", "")

            query.setResult(0, int(max_top_n) - 1)
            query.setFrom(collection)
            query.setSearch(True)
            query.setDebug(True)
            query.setPrintQuery(True)
            query.setLoggable(True)

            query.setValue("VS_THRESHOLD", str(threshold))
            query.setValue("VS_RESULT_SIZE", str(max_top_n))

            query.setSelect(select_set_array)

            # 정렬: WEIGHT 수치 내림차순 (JByte(97) = 수치 정렬)
            order_set_array = [jpkg_query.OrderBySet(True, "WEIGHT", jpype.JByte(97))]
            query.setOrderby(order_set_array)

            # WHERE: 5개 필드 OR 검색 + 스크립틀릿 필터
            ks = search_strings[i]
            where_set_array = [
                jpkg_query.WhereSet(MARINER_WS_OR),                                        # OR (
                jpkg_query.WhereSet("BUSINESS_NAME_KO", MARINER_WS_BM25,  ks, MARINER_WEIGHT_HIGH),         #   사업명 키워드
                jpkg_query.WhereSet(MARINER_WS_AND),                                        #   OR
                jpkg_query.WhereSet("TEXT_CHUNK_KO",    2,  ks, MARINER_WEIGHT_HIGH),         #   텍스트 키워드
                jpkg_query.WhereSet(MARINER_WS_AND),                                        #   OR
                jpkg_query.WhereSet("BUSINESS_NAME_MI", MARINER_WS_BM25,  ks, biz_name_mi_weight),  # 사업명 벡터
                jpkg_query.WhereSet(MARINER_WS_AND),                                        #   OR
                jpkg_query.WhereSet("TEXT_CHUNK_MI",    96, ks, MARINER_WEIGHT_MED),         #   텍스트 벡터
                jpkg_query.WhereSet(MARINER_WS_AND),                                        #   OR
                jpkg_query.WhereSet("SIGUN",            96, ks, MARINER_WEIGHT_LOW),         #   시군 벡터
                jpkg_query.WhereSet(MARINER_WS_END),                                       # )
            ]

            # SIGUN 스크립틀릿 필터
            if sigun_scriptlet:
                where_set_array += [
                    jpkg_query.WhereSet(MARINER_WS_FILTER),
                    jpkg_query.WhereSet("SIGUN", MARINER_WS_EXACT, sigun_scriptlet, 0),
                ]

            # LIFE_CYCLE 스크립틀릿 필터
            if lifecycle_filter:
                where_set_array += [
                    jpkg_query.WhereSet(MARINER_WS_FILTER),
                    jpkg_query.WhereSet("LIFE_CYCLE", 34, lifecycle_filter, 0),
                ]

            query.setWhere(where_set_array)

            # YEAR FilterSet (연도 필터가 있는 경우)
            if year_filters:
                year_ranges = []
                for y in year_filters:
                    y = str(y).strip()
                    if y:
                        year_ranges.extend([f"{y}-01-01", f"{y}-12-31"])
                if year_ranges:
                    filter_set_array = [
                        jpkg_query.FilterSet(
                            jpype.JByte(3), "YEAR",
                            jpype.JArray(jpype.JString)(year_ranges), 0
                        )
                    ]
                    query.setFilter(filter_set_array)

            queryset.addQuery(query)

        ret = command.request(queryset)

        if ret < 0:
            logger.error(f"[Mariner/general/SEP] 요청 오류: {ret}")
            raise RAGServiceError(f"Mariner API 반환 코드: {ret}")

        resultSet = command.getResultSet()
        if resultSet is None:
            logger.warning("[Mariner/general/SEP] 서버에서 결과를 받지 못했습니다.")
            return [], []

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

        # getResult(0): TEXT_CHUNK_KO (키워드), getResult(1): TEXT_CHUNK_MI (벡터)
        keyword_docs = []
        vector_docs = []
        for result_idx, doc_list_ref, label in [
            (0, keyword_docs, "keyword"),
            (1, vector_docs, "vector"),
        ]:
            result = resultSet.getResult(result_idx)
            result_size = result.getRealSize()
            logger.info(f"[Mariner/general/SEP] [{label}] raw 결과: {result_size}개 (필터 전)")

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

                doc_list_ref.append(doc)

        t2 = time.monotonic()
        logger.info(
            f"[Mariner/general/SEP] 검색 시간: {t2 - t1:.3f}초, "
            f"keyword 결과: {len(keyword_docs)}개, vector 결과: {len(vector_docs)}개"
        )

        return keyword_docs, vector_docs

    except RAGServiceError:
        raise
    except Exception as e:
        logger.error(f"[Mariner/general/SEP] 예상치 못한 오류: {e}", exc_info=True)
        raise RAGServiceError(f"문서 검색 중 오류: {str(e)}") from e
