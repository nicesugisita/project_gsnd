"""Mariner JPype 검색 엔진 인터페이스"""

import glob
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import jpype
from jpype import JString

from app.core.config import Config
from app.core.constants import (
    MARINER_SELECT_FIELD_NUM,
    MARINER_SETPROPS_EXTRA,
    OP_OR,
    OP_HASANY,
    OP_VECTOR_SEARCH,
)
from app.core.exceptions import RAGServiceError
from app.mariner.sigun_utils import normalize_sigun

from app.chat.infra.rag.collection import (
    _uses_okms_document_schema,
    _uses_gsnd_v7_schema,
    _uses_welfare_center_schema,
)
from app.chat.infra.rag.document import _build_okms_document_name
from app.chat.infra.rag.extraction import _LIFECYCLE_CONTENT_KEYWORDS

logger = logging.getLogger(__name__)

_jvm_lock = threading.Lock()


def _get_jar_files() -> List[str]:
    """JAR 라이브러리 파일 경로 조회"""
    return glob.glob(os.path.join(Config.JAR_LIB_PATH, '*.jar'))


def query_mariner_documents(
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
    Mariner JPype API를 통해 관련 문서 검색

    Args:
        keyword: 검색 키워드
        collection: 컬렉션 이름 (미지정 시 기본값 사용)
        user_id: 업로드 문서 검색 제한용 사용자 ID
        conv_id: 업로드 문서 검색 제한용 대화 ID
        year_filters: YEAR 필드 필터 목록
        sigun_filters: SIGUN 필드 필터 목록
        lifecycle_filter: LIFE_CYCLE 필드 포함 여부 키워드
        facility_type_filter: FACILITY_TYPE 필드 필터
        search_mode: 검색 모드 ("hybrid", "vector", "keyword")

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

        jar_files = _get_jar_files()
        if not jar_files:
            logger.error(f"JAR 파일을 찾을 수 없습니다: {Config.JAR_LIB_PATH}")
            raise RAGServiceError(f"JAR 라이브러리 경로 오류: {Config.JAR_LIB_PATH}")

        with _jvm_lock:
            if not jpype.isJVMStarted():
                jpype.startJVM(
                    jpype.getDefaultJVMPath(),
                    f"-Djava.class.path={':'.join(jar_files)}",
                    convertStrings=True,
                )

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
        query.setSelect(select_set_array)

        order_set_array = [jpkg_query.OrderBySet(True, "WEIGHT")]
        query.setOrderby(order_set_array)

        keyword_string = JString(keyword)
        if search_mode == "keyword":
            ko_weight, mi_weight = 0.99, 0.01
        elif search_mode == "vector":
            ko_weight, mi_weight = 0.01, 0.99
        else:
            ko_weight, mi_weight = 0.2, 0.8
        where_set_array = [
            jpkg_query.WhereSet("TEXT_CHUNK_KO", OP_HASANY, keyword_string, ko_weight),
            jpkg_query.WhereSet(OP_OR),
            jpkg_query.WhereSet("TEXT_CHUNK_MI", OP_VECTOR_SEARCH, keyword_string, mi_weight)
        ]
        try:
            from app.chat.infra.rag.stage_trace import record_search_query as _stage_rec_sq
            _stage_rec_sq("UPLOAD", where_set_array)
        except Exception:  # noqa: BLE001
            pass
        query.setWhere(where_set_array)

        queryset = jpkg_query.QuerySet(1)
        queryset.addQuery(query)
        ret = command.request(queryset)

        if ret < 0:
            logger.error(f"Mariner 요청 오류: {ret}")
            raise RAGServiceError(f"Mariner API 반환 코드: {ret}")

        resultSet = command.getResultSet()
        if resultSet is None:
            logger.warning("Mariner 서버에서 결과를 받지 못했습니다.")
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
        logger.info(f"[Mariner] raw 결과: {result_size}개 (필터 전), 키워드: {keyword[:50]}, sigun_filters={list(target_siguns) if target_siguns else None}")

        for i in range(result_size):
            try:
                raw_weight = result.getResult(i, field_indexes["WEIGHT"])
                weight_val = float(str(raw_weight)) * 0.0001
            except (ValueError, TypeError):
                weight_val = 0.0

            if weight_val < 0.5:
                continue

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
        logger.info(f"[Mariner] 검색 시간: {t2 - t1:.3f}초, 키워드: {keyword[:50]}, 결과: {len(doc_list)}개")
        for i, doc in enumerate(doc_list, 1):
            logger.debug(f"[Mariner] #{i} CHUNK_ID={doc.get('CHUNK_ID', '?')}, NAME={doc.get('NAME', '?')}, WEIGHT={doc.get('WEIGHT', '?')}")

        return doc_list

    except RAGServiceError:
        raise
    except Exception as e:
        logger.error(f"[Mariner] 예상치 못한 오류: {e}", exc_info=True)
        raise RAGServiceError(f"문서 검색 중 오류: {str(e)}") from e
