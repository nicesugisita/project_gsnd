"""
Mariner 쿼리셋 — GSND_DATASET_V8 전용 (Fallback)

"""

import logging
import time
from typing import Dict, Any, List, Optional

import jpype
from jpype import JString

from app.core.config import Config
from app.core.constants import (
    MARINER_SELECT_FIELD_NUM,
    MARINER_SETPROPS_EXTRA,
    OP_BRACE_OPEN,
    OP_OR,
    OP_BRACE_CLOSE,
    OP_HASANY, OP_VECTOR_SEARCH,
    MARINER_WEIGHT_HIGH,
    MARINER_WEIGHT_MED,
)
from app.core.exceptions import RAGServiceError
from app.mariner.jvm_manager import ensure_jvm_thread

logger = logging.getLogger(__name__)


# ============================================================
# GSND_DATASET_V8 Fallback 검색
# ============================================================

def query_GSND_general_documents_fallback(
    keyword: str,
    collection: str = None,
    user_id: Optional[str] = None,
    conv_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    GSND Fallback — 필터 없이 검색 (QuerySet(1))

    SIGUN 스크립틀릿, COMPLI_DT FilterSet, Python 후처리 필터 없이
    4-field OR 검색만 수행합니다.

    Args:
        keyword: 검색 키워드
        collection: 컬렉션 이름 (미지정 시 기본값 사용)
        user_id: 업로드 문서 검색 제한용 사용자 ID
        conv_id: 업로드 문서 검색 제한용 대화 ID

    Returns:
        검색된 문서 목록

    Raises:
        RAGServiceError: JPype 또는 Mariner 호출 실패 시
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
        logger.info(
            f"[query_gsdn keywords] keywords = {keyword}")
        keyword_string = JString(keyword)

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

        # 정렬: COMPLI_DT 내림차순 (JByte(97))
        order_set_array = [jpkg_query.OrderBySet(True, "COMPLI_DT", jpype.JByte(97))]
        query.setOrderby(order_set_array)

        # WHERE: 4-field OR 검색식 (필터 없음)
        where_set_array = [
            jpkg_query.WhereSet(OP_BRACE_OPEN),                                        # OR (
            jpkg_query.WhereSet("NAME_KO",        2,  keyword_string, MARINER_WEIGHT_MED),
            jpkg_query.WhereSet(OP_OR),                                        #   OR
            jpkg_query.WhereSet("TEXT_CHUNK_KO", OP_HASANY,  keyword_string, MARINER_WEIGHT_MED),
            jpkg_query.WhereSet(OP_OR),                                        #   OR
            jpkg_query.WhereSet("NAME_MI",        2,  keyword_string, MARINER_WEIGHT_HIGH),
            jpkg_query.WhereSet(OP_OR),                                        #   OR
            jpkg_query.WhereSet("TEXT_CHUNK_MI", OP_VECTOR_SEARCH, keyword_string, MARINER_WEIGHT_HIGH),
            jpkg_query.WhereSet(OP_BRACE_CLOSE),                                       # )
        ]

        # Fallback: SIGUN 스크립틀릿, COMPLI_DT FilterSet 미적용

        query.setWhere(where_set_array)

        queryset = jpkg_query.QuerySet(1)
        queryset.addQuery(query)
        ret = command.request(queryset)

        if ret < 0:
            logger.error(f"[Mariner/GSND/FB] 요청 오류: {ret}")
            raise RAGServiceError(f"Mariner API 반환 코드: {ret}")

        resultSet = command.getResultSet()
        if resultSet is None:
            logger.warning("[Mariner/GSND/FB] 서버에서 결과를 받지 못했습니다.")
            return []

        result = resultSet.getResult(0)
        result_size = result.getRealSize()
        doc_list = []
        logger.info(f"[Mariner/GSND/FB] raw 결과: {result_size}개, 키워드: {keyword[:50]}")

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

            doc_list.append(doc)

        t2 = time.monotonic()
        logger.info(f"[Mariner/GSND/FB] 검색 시간: {t2 - t1:.3f}초, 키워드: {keyword[:50]}, 결과: {len(doc_list)}개")
        for i, doc in enumerate(doc_list, 1):
            logger.debug(f"[Mariner/GSND/FB] #{i} CHUNK_ID={doc.get('CHUNK_ID', '?')}, NAME={doc.get('NAME', '?')}, WEIGHT={doc.get('WEIGHT', '?')}")

        return doc_list

    except RAGServiceError:
        raise
    except Exception as e:
        logger.error(f"[Mariner/GSND/FB] 예상치 못한 오류: {e}", exc_info=True)
        raise RAGServiceError(f"문서 검색 중 오류: {str(e)}") from e
