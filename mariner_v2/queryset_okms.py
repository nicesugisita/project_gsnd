"""
Mariner 쿼리셋 — TEST_OKMS_V4 듀얼 검색 (Group A/B)

Group A(균등 가중치)와 Group B(KO/MI 가중치 분리) 듀얼 검색 함수입니다.
general, guide_recommend 의도에서 공유합니다.

기존 mariner/queryset.py, services/rag_service.py는 변경하지 않습니다.
"""

import logging
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
    MARINER_WS_NOT,
    MARINER_WS_BM25,
    MARINER_WS_EXACT,
    MARINER_WEIGHT_HIGH,
    MARINER_WEIGHT_MED,
    MARINER_WEIGHT_LOW,
)
from core.exceptions import RAGServiceError
from mariner_v2.jvm_manager import ensure_jvm_thread

logger = logging.getLogger(__name__)


def _build_okms_document_name(doc: Dict[str, Any]) -> str:
    """OKMS 문서의 UI 표시용 이름 생성 (ORG_NM 우선, 없으면 BUSINESS_NAME, 없으면 SIGUN/YEAR)"""
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


# ============================================================
# 듀얼 검색 내부 공통 함수 (Group A/B 공유)
# ============================================================

def _query_dual_documents(
    vector: str,
    keyword: str,
    collection: str = None,
    user_id: Optional[str] = None,
    conv_id: Optional[str] = None,
    year_filters: Optional[List[str]] = None,
    sigun_filters: Optional[List[str]] = None,
    lifecycle_filter: Optional[str] = None,
    excluded_chunk_ids: Optional[List[str]] = None,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    QuerySet(2) 듀얼 검색 공통 로직 — TEST_OKMS_V4 전용.

    Query[0]: keyword(트리플쿼리), Query[1]: vector(확장쿼리)
    KO 필드 가중치 0.7 / MI 필드 가중치 0.3

    Returns:
        (keyword_docs, vector_docs) 튜플
    """
    log_label = "GroupA"
    t1 = time.monotonic()

    if not Config.RAG_ENABLED:
        logger.warning("RAG가 비활성화되어 있습니다.")
        return [], []

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
            "[MoreResults][Mariner/%s] 제외 입력 수=%d | 샘플=%s",
            log_label,
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

        # keyword가 비어 있으면 벡터 전용(QuerySet(1)), 있으면 듀얼(QuerySet(2))
        use_dual = bool(keyword.strip())
        num_queries = 2 if use_dual else 1
        queryset = jpkg_query.QuerySet(num_queries)

        # SELECT 필드 (TEST_OKMS_V4 전용)
        num = MARINER_SELECT_FIELD_NUM
        select_field_names = ["ID", "YEAR", "SIGUN", "CONTENT", "WEIGHT", "LIFE_CYCLE", "DEPARTMENT", "APPLICATION_PERIOD", "PURPOSE", "TEL", "BUSINESS_NAME", "ORG_NM", "PATH"]

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
        # biz_name_mi_weight = 2.0 if has_sigun else MARINER_WEIGHT_HIGH

        # sigun 스크립틀릿용 값 ("경상남도" 제외한 실제 시군 목록)
        sigun_scriptlet_values = [
            s for s in (sigun_filters or [])
            if s and s != "경상남도"
        ]

        # use_dual=True: Query[0]=keyword, Query[1]=vector
        # use_dual=False: Query[0]=vector 전용
        logger.debug(f"[query_okms keywords] keywords = {keyword} / vector = {vector}")
        search_strings = [JString(keyword), JString(vector)] if use_dual else [JString(vector)]

        # KO 필드 가중치 0.7, MI 필드 가중치 0.3
        uniform = {"biz_ko": MARINER_WEIGHT_HIGH, "txt_ko": MARINER_WEIGHT_HIGH, "biz_mi": MARINER_WEIGHT_MED, "txt_mi": MARINER_WEIGHT_MED, "sigun": MARINER_WEIGHT_LOW}
        weight_sets = [uniform] * num_queries

        for i in range(num_queries):
            ks = search_strings[i]
            if not ks or not str(ks).strip():
                logger.info(f"[Mariner/{log_label}] Query #{i} 검색어가 비어 있습니다. 검색을 건너뜁니다.")
                continue
            ws = weight_sets[i]
            query = jpkg_query.Query("", "")

            query.setResult(0, int(max_top_n) - 1)
            query.setSearchKeyword(ks)
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

            where_set_array = [
                jpkg_query.WhereSet(MARINER_WS_OR),                                              # OR (
                jpkg_query.WhereSet("BUSINESS_NAME_KO", MARINER_WS_BM25,  ks, ws["biz_ko"]),      #   사업명 키워드
                jpkg_query.WhereSet(MARINER_WS_AND),                                              #   OR
                # jpkg_query.WhereSet("TEXT_CHUNK_KO",    2,  ks, ws["txt_ko"]),      #   텍스트 키워드
                jpkg_query.WhereSet("TEXT_CHUNK_KO",    2,  ks, 0.5),      #   텍스트 키워드
                jpkg_query.WhereSet(MARINER_WS_AND),                                              #   OR
                jpkg_query.WhereSet("BUSINESS_NAME_MI", MARINER_WS_BM25,  ks, ws["biz_mi"]),      #   사업명 벡터
                jpkg_query.WhereSet(MARINER_WS_AND),                                              #   OR
                jpkg_query.WhereSet("TEXT_CHUNK_MI",    96, ks, ws["txt_mi"]),      #   텍스트 벡터
                jpkg_query.WhereSet(MARINER_WS_AND),                                              #   OR
                jpkg_query.WhereSet("SIGUN",            96, ks, ws["sigun"]),       #   시군 벡터
                jpkg_query.WhereSet(MARINER_WS_END),                                             # )
            ]

            # SIGUN 스크립틀릿 필터 (n개 OR)
            if sigun_scriptlet_values:
                if len(sigun_scriptlet_values) == 1:
                    where_set_array += [
                        jpkg_query.WhereSet(MARINER_WS_FILTER),
                        jpkg_query.WhereSet("SIGUN", MARINER_WS_EXACT, sigun_scriptlet_values[0], 0),
                    ]
                else:
                    where_set_array.append(jpkg_query.WhereSet(MARINER_WS_FILTER))
                    where_set_array.append(jpkg_query.WhereSet(MARINER_WS_OR))  # OR (
                    for idx, sv in enumerate(sigun_scriptlet_values):
                        if idx > 0:
                            where_set_array.append(jpkg_query.WhereSet(MARINER_WS_AND))  # OR
                        where_set_array.append(jpkg_query.WhereSet("SIGUN", MARINER_WS_EXACT, sv, 0))
                    where_set_array.append(jpkg_query.WhereSet(MARINER_WS_END))  # )

            # LIFE_CYCLE 스크립틀릿 필터
            if lifecycle_filter:
                where_set_array += [
                    jpkg_query.WhereSet(MARINER_WS_FILTER),
                    jpkg_query.WhereSet("LIFE_CYCLE", 34, lifecycle_filter, 0),
                ]

            # CHUNK_ID 제외 필터 (예제 패턴: NOT + EXACT 반복)
            if excluded_chunk_set:
                excluded_values = sorted(excluded_chunk_set)
                logger.debug(
                    "[MoreResults][Mariner/%s] 검색단 제외 IDs(%d): %s",
                    log_label,
                    len(excluded_values),
                    excluded_values,
                )
                for chunk_id in excluded_values:
                    where_set_array += [
                        jpkg_query.WhereSet(MARINER_WS_NOT),
                        jpkg_query.WhereSet("ID", MARINER_WS_EXACT, chunk_id, 0),
                    ]

            query.setWhere(where_set_array)

            # YEAR FilterSet (사용자가 명시한 경우만 필터, 없으면 전체 연도)
            if year_filters:
                filter_years = sorted(set(str(y).strip() for y in year_filters if str(y).strip()))
            else:
                filter_years = []  # 명시 안 하면 필터 없음 (전체 연도)

            if filter_years:
                min_year = filter_years[0]
                max_year = filter_years[-1]
                filter_set_array = [
                    jpkg_query.FilterSet(
                        jpype.JByte(3), "YEAR",
                        jpype.JArray(jpype.JString)([f"{min_year}0101", f"{max_year}1231"]), 0
                    )
                ]
                query.setFilter(filter_set_array)
                logger.debug(f"[Mariner/{log_label}] YEAR FilterSet: {min_year}~{max_year}")

            queryset.addQuery(query)

        ret = command.request(queryset)

        if ret < 0:
            logger.error(f"[Mariner/{log_label}] 요청 오류: {ret}")
            if sigun_filters == None :
                sigun_filters = 'null'
            if lifecycle_filter == None:
                lifecycle_filter = 'null'
            if year_filters == None:
                year_filters = 'null' 
            raise RAGServiceError(f"Mariner API 반환 코드: {ret}")

        resultSet = command.getResultSet()
        if resultSet is None:
            logger.warning(f"[Mariner/{log_label}] 서버에서 결과를 받지 못했습니다.")
            return [], []

        # use_dual=True:  getResult(0)=keyword, getResult(1)=vector
        # use_dual=False: getResult(0)=vector 전용
        keyword_docs = []
        vector_docs = []
        result_pairs = (
            [(0, keyword_docs, "keyword"), (1, vector_docs, "vector")]
            if use_dual else
            [(0, vector_docs, "vector")]
        )
        for result_idx, doc_list_ref, label in result_pairs:
            result = resultSet.getResult(result_idx)
            result_size = result.getRealSize()
            logger.info(f"[Mariner/{log_label}] [{label}] raw 결과: {result_size}개 (필터 전)")
            excluded_count = 0
            raw_id_samples: List[str] = []
            removed_id_samples: List[str] = []

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

                if excluded_chunk_set:
                    result_chunk_id = str(result.getResult(i, field_indexes["ID"]) or "").strip()
                    if result_chunk_id and len(raw_id_samples) < 10:
                        raw_id_samples.append(result_chunk_id)
                    if result_chunk_id and result_chunk_id in excluded_chunk_set:
                        excluded_count += 1
                        if len(removed_id_samples) < 20:
                            removed_id_samples.append(result_chunk_id)
                        continue

                doc = {field_name: str(result.getResult(i, idx) or "") for field_name, idx in field_indexes.items()}
                doc["WEIGHT"] = str(weight_val)

                # TEST_OKMS_V4 필드 매핑
                doc["CHUNK_ID"] = doc.get("ID", "")
                doc["NAME"] = _build_okms_document_name(doc)
                doc["CHUNK_PATH"] = str(doc.get("CONTENT", "") or "")

                doc_list_ref.append(doc)

            if excluded_chunk_set:
                logger.info(
                    f"[MoreResults][Mariner/{log_label}] [{label}] CHUNK_ID 1차 제외: {excluded_count}개"
                )
                logger.info(
                    "[MoreResults][Mariner/%s] [%s] raw ID 샘플=%s",
                    log_label,
                    label,
                    raw_id_samples,
                )
                logger.info(
                    "[MoreResults][Mariner/%s] [%s] 실제 제외 ID 샘플=%s",
                    log_label,
                    label,
                    removed_id_samples[:10],
                )

        t2 = time.monotonic()
        logger.info(
            f"[Mariner/{log_label}] 검색 시간: {t2 - t1:.3f}초, "
            f"keyword 결과: {len(keyword_docs)}개, vector 결과: {len(vector_docs)}개"
        )

        return keyword_docs, vector_docs

    except RAGServiceError:
        raise
    except Exception as e:
        logger.error(f"[Mariner/{log_label}] 예상치 못한 오류: {e}", exc_info=True)
        raise RAGServiceError(f"문서 검색 중 오류: {str(e)}") from e


# ============================================================
# Group A — 균등 가중치 듀얼 검색
# ============================================================

def query_group_a_documents(
    vector: str,
    keyword: str,
    collection: str = None,
    user_id: Optional[str] = None,
    conv_id: Optional[str] = None,
    year_filters: Optional[List[str]] = None,
    sigun_filters: Optional[List[str]] = None,
    lifecycle_filter: Optional[str] = None,
    excluded_chunk_ids: Optional[List[str]] = None,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Group A — 듀얼 검색 (QuerySet(2))

    keyword(트리플쿼리)와 vector(확장쿼리)를 동시에 Mariner에 전송합니다.
    KO 필드 가중치 0.7 / MI 필드 가중치 0.3

    Returns:
        (keyword_docs, vector_docs) 튜플
    """
    return _query_dual_documents(
        vector, keyword, collection,
        user_id=user_id, conv_id=conv_id,
        year_filters=year_filters,
        sigun_filters=sigun_filters,
        lifecycle_filter=lifecycle_filter,
        excluded_chunk_ids=excluded_chunk_ids,
    )
