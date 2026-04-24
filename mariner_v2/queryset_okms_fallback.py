"""
Mariner 쿼리셋 — TEST_OKMS_V4 듀얼 검색 (Fallback)

연도/생애주기 필터를 제거하고 검색하는 fallback용 쿼리셋입니다.
- SIGUN 스크립틀릿: 지역명이 있을 경우에만 적용
- LIFE_CYCLE 스크립틀릿: 미적용
- YEAR FilterSet: 미적용

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


_LIFECYCLE_CONTENT_KEYWORDS: Dict[str, List[str]] = {
    "영유아": ["영유아"],
    "아동": ["아동"],
    "청소년": ["청소년"],
    "청년": ["청년"],
    "중장년": ["중장년"],
    "노인": ["노인", "노년"],
}


# ============================================================
# 듀얼 검색 내부 공통 함수 (Group A/B 공유)
# ============================================================

def _query_dual_documents(
    vector: str,
    keyword: str,
    collection: str = None,
    user_id: Optional[str] = None,
    conv_id: Optional[str] = None,
    sigun_filters: Optional[List[str]] = None,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    QuerySet(2) 듀얼 검색 공통 로직 — TEST_OKMS_V4 Fallback 전용.

    - SIGUN 스크립틀릿: sigun_filters가 있을 경우에만 적용
    - LIFE_CYCLE 스크립틀릿: 미적용
    - YEAR FilterSet: 미적용
    - KO 필드 가중치 0.7 / MI 필드 가중치 0.3

    Returns:
        (keyword_docs, vector_docs) 튜플
    """
    log_label = "GroupA/FB"
    t1 = time.monotonic()

    if not Config.RAG_ENABLED:
        logger.warning("RAG가 비활성화되어 있습니다.")
        return [], []

    if collection is None:
        collection = Config.RAG_COLLECTION

    if not keyword or not str(keyword).strip():
        logger.warning(f"[Mariner/GroupA/FB] keyword가 비어 있어 검색을 건너뜁니다.")
        return [], []
    if not vector or not str(vector).strip():
        logger.warning(f"[Mariner/GroupA/FB] vector가 비어 있어 검색을 건너뜁니다.")
        return [], []


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

        has_sigun = bool(sigun_filters and any(s != "경상남도" for s in sigun_filters))

        # sigun 스크립틀릿용 값 ("경상남도" 제외한 실제 시군 목록)
        sigun_scriptlet_values = [
            s for s in (sigun_filters or [])
            if s and s != "경상남도"
        ]

        # Query[0]: keyword(트리플쿼리) 사용, Query[1]: vector(확장쿼리) 사용
        logger.info(
            f"[query_okms keywords] keywords = {keyword} / vector = {vector}")
        search_strings = [JString(keyword), JString(vector)]

        # KO 필드 가중치 0.7, MI 필드 가중치 0.3
        uniform = {"biz_ko": MARINER_WEIGHT_HIGH, "txt_ko": MARINER_WEIGHT_HIGH, "biz_mi": MARINER_WEIGHT_MED, "txt_mi": MARINER_WEIGHT_MED, "sigun": MARINER_WEIGHT_LOW}
        weight_sets = [uniform, uniform]

        for i in range(2):
            query = jpkg_query.Query("", "")

            ks = search_strings[i]

            ws = weight_sets[i]
            query.setResult(0, int(max_top_n) - 1)
            query.setFrom(collection)
            query.setSearchKeyword(ks)
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
            where_set_array = []
            if not keyword or not str(keyword).strip():
                logger.warning(f"[Mariner/GroupA/FB] keyword가 비어 있어 검색을 건너뜁니다.")
            else:
                where_set_array = [
                    jpkg_query.WhereSet(MARINER_WS_OR),                                              # OR (
                    jpkg_query.WhereSet("BUSINESS_NAME_KO", MARINER_WS_BM25,  ks, ws["biz_ko"]),      #   사업명 키워드
                    jpkg_query.WhereSet(MARINER_WS_AND),                                              #   OR
                    jpkg_query.WhereSet("TEXT_CHUNK_KO",    2,  ks, ws["txt_ko"]),      #   텍스트 키워드
                    jpkg_query.WhereSet(MARINER_WS_AND),                                              #   OR
                    jpkg_query.WhereSet("BUSINESS_NAME_MI", MARINER_WS_BM25,  ks, ws["biz_mi"]),      #   사업명 벡터
                    jpkg_query.WhereSet(MARINER_WS_AND),                                              #   OR
                    jpkg_query.WhereSet("TEXT_CHUNK_MI",    96, ks, ws["txt_mi"]),      #   텍스트 벡터
                    jpkg_query.WhereSet(MARINER_WS_AND),                                              #   OR
                    jpkg_query.WhereSet("SIGUN",            96, ks, ws["sigun"]),       #   시군 벡터
                    jpkg_query.WhereSet(MARINER_WS_END),                                             # )
                ]

            # Fallback: SIGUN 스크립틀릿은 지역명 있을 때만 (n개 OR), LIFE_CYCLE/YEAR 미적용
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
                logger.debug(f"[Mariner/{log_label}] Fallback SIGUN 스크립틀릿 적용: {sigun_scriptlet_values}")

            query.setWhere(where_set_array)
            queryset.addQuery(query)

        ret = command.request(queryset)

        if ret < 0:
            logger.error(f"[Mariner/{log_label}] 요청 오류: {ret}")
            raise RAGServiceError(f"Mariner API 반환 코드: {ret}")

        resultSet = command.getResultSet()
        if resultSet is None:
            logger.warning(f"[Mariner/{log_label}] 서버에서 결과를 받지 못했습니다.")
            return [], []

        # getResult(0): keyword 결과, getResult(1): vector 결과
        keyword_docs = []
        vector_docs = []
        for result_idx, doc_list_ref, label in [
            (0, keyword_docs, "keyword"),
            (1, vector_docs, "vector"),
        ]:
            result = resultSet.getResult(result_idx)
            result_size = result.getRealSize()
            logger.info(f"[Mariner/{log_label}] [{label}] raw 결과: {result_size}개 (필터 전)")

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

                # TEST_OKMS_V4 필드 매핑
                doc["CHUNK_ID"] = doc.get("ID", "")
                doc["NAME"] = _build_okms_document_name(doc)
                doc["CHUNK_PATH"] = str(doc.get("CONTENT", "") or "")

                doc_list_ref.append(doc)

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

def query_group_a_fallback(
    vector: str,
    keyword: str,
    collection: str = None,
    user_id: Optional[str] = None,
    conv_id: Optional[str] = None,
    sigun_filters: Optional[List[str]] = None,
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Group A Fallback — 듀얼 검색 (QuerySet(2))

    LIFE_CYCLE/YEAR 필터 없이 검색합니다.
    sigun_filters가 있으면 SIGUN 스크립틀릿만 적용합니다.
    KO 필드 가중치 0.7 / MI 필드 가중치 0.3

    Returns:
        (keyword_docs, vector_docs) 튜플
    """
    return _query_dual_documents(
        vector, keyword, collection,
        user_id=user_id, conv_id=conv_id,
        sigun_filters=sigun_filters,
    )
