"""
Mariner 쿼리셋 — GSND_WELFARE_CENTER_V1 전용

복지시설 컬렉션 검색에 사용되는 Mariner 쿼리 함수입니다.
예제 검색식(MarinerQuerySetExampleCode_WELFARE.py)을 기반으로
4-field OR 검색식을 적용합니다.

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
    MARINER_WS_NOT,
    MARINER_WS_EXACT,
    MARINER_WS_VECTOR,
    MARINER_WEIGHT_HIGH,
    MARINER_WEIGHT_MED,
)
from core.exceptions import RAGServiceError
from mariner.queryset import normalize_sigun
from mariner_v2.jvm_manager import ensure_jvm_thread

logger = logging.getLogger(__name__)


# ============================================================
# WELFARE_CENTER 전용 Mariner 검색 함수
# ============================================================

def query_welfare_center_documents(
    keyword: str,
    sigun_filters: Optional[List[str]] = None,
    facility_type_filter: Optional[str] = None,
    excluded_chunk_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    GSND_WELFARE_CENTER_V1 컬렉션 전용 Mariner 검색

    예제 검색식(MarinerQuerySetExampleCode_WELFARE.py) 기반:
    - WHERE: 4-field OR (TEXT_CHUNK_KO, TEXT_CHUNK_MI, FACILITY_NAME, SIGUN)
    - SIGUN 스크립틀릿: op 1
    - OrderBy: WEIGHT 수치 내림차순 (JByte(97))

    Args:
        keyword: 검색 키워드
        sigun_filters: SIGUN 필드 필터 목록 (예: ["경상남도 창원시"])
        facility_type_filter: 시설 유형 필터 (예: "노인복지관")

    Returns:
        검색된 복지시설 문서 목록

    Raises:
        RAGServiceError: JPype 또는 Mariner 호출 실패 시
    """
    t1 = time.monotonic()

    if not Config.RAG_ENABLED:
        logger.warning("RAG가 비활성화되어 있습니다.")
        return []

    collection = Config.RAG_WELFARE_CENTER_COLLECTION

    excluded_chunk_set = {
        str(chunk_id).strip()
        for chunk_id in (excluded_chunk_ids or [])
        if str(chunk_id).strip()
    }
    if excluded_chunk_set:
        excluded_values = sorted(excluded_chunk_set)
        logger.info(
            "[MoreResults][Mariner/welfare] 제외 입력 수=%d | 샘플=%s",
            len(excluded_values),
            excluded_values[:10],
        )

    try:
        # Mariner 설정 — 예제 코드 기준값 사용 (threshold=0.2, top_n=20, vs_size=50)
        timeout = Config.MARINER_TIMEOUT
        threshold = 0.2
        top_n = 20
        vs_result_size = 50

        # JAR 파일 로드
        ensure_jvm_thread()

        # Mariner 쿼리 구성
        jpkg_cmd = jpype.JPackage("com.diquest.ir5.client.command")
        command = jpkg_cmd.CommandSearchRequest(Config.MARINER_IP, int(Config.MARINER_PORT))
        command.setProps(Config.MARINER_IP, int(Config.MARINER_PORT), timeout, MARINER_SETPROPS_EXTRA, MARINER_SETPROPS_EXTRA)

        jpkg_query = jpype.JPackage("com.diquest.ir5.common.msg.protocol.query")
        query = jpkg_query.Query("", "")
        keyword_string = JString(keyword)
        logger.info(f"[query_welfare keywords] keywords = {keyword}")

        # 결과 범위 설정
        startnum = 0
        endnum = int(top_n) - 1
        query.setResult(startnum, endnum)
        query.setFrom(collection)
        query.setSearch(True)
        query.setDebug(True)
        query.setPrintQuery(True)
        query.setLoggable(True)

        # 검색 파라미터
        query.setValue("VS_THRESHOLD", str(threshold))
        query.setValue("VS_RESULT_SIZE", str(vs_result_size))

        # SELECT 필드 (WELFARE_CENTER 전용)
        num = MARINER_SELECT_FIELD_NUM
        select_field_names = [
            "ID", "SIGUN", "FACILITY_NAME", "ADDRESS",
            "FACILITY_TYPE", "FACILITY_CATEGORY", "FACILITY_CAPACITY",
            "HOMEPAGE", "TEL", "WEIGHT",
        ]

        select_set_array = [
            jpkg_query.SelectSet(JString(field_name), num, 0)
            for field_name in select_field_names
        ]
        field_indexes = {field_name: idx for idx, field_name in enumerate(select_field_names)}
        query.setSelect(select_set_array)

        # WHERE: 4-field OR 검색식 (op 코드는 예제 MarinerQuerySetExampleCode_WELFARE.py 기준)
        where_set_array = [
            jpkg_query.WhereSet(MARINER_WS_OR),                                        # OR (
            jpkg_query.WhereSet("TEXT_CHUNK_KO",   2,  keyword_string, MARINER_WEIGHT_HIGH),  # BM25 벡터
            jpkg_query.WhereSet(MARINER_WS_AND),                                        #   OR
            jpkg_query.WhereSet("TEXT_CHUNK_MI", MARINER_WS_VECTOR,  keyword_string, MARINER_WEIGHT_MED),  # 벡터 유사도
            jpkg_query.WhereSet(MARINER_WS_AND),                                        #   OR
            jpkg_query.WhereSet("FACILITY_NAME",   2,  keyword_string, MARINER_WEIGHT_HIGH),  # BM25 벡터
            jpkg_query.WhereSet(MARINER_WS_AND),                                        #   OR
            jpkg_query.WhereSet("SIGUN",           1,  keyword_string, MARINER_WEIGHT_HIGH),  # 키워드
            jpkg_query.WhereSet(MARINER_WS_END),                                       # )
        ]

        # CHUNK_ID(ID) 제외 필터 (예제 패턴: NOT + EXACT 반복)
        if excluded_chunk_set:
            excluded_values = sorted(excluded_chunk_set)
            logger.debug(
                "[MoreResults][Mariner/welfare] 검색단 제외 IDs(%d): %s",
                len(excluded_values),
                excluded_values,
            )
            for chunk_id in excluded_values:
                where_set_array += [
                    jpkg_query.WhereSet(MARINER_WS_NOT),
                    jpkg_query.WhereSet("ID", MARINER_WS_EXACT, chunk_id, 0),
                ]

        # SIGUN 스크립틀릿: Mariner 레벨 필터 미적용 — Python 후처리로만 필터링
        # WELFARE_CENTER SIGUN 필드 포맷 미확인 (단축형/전체형 불명), 스크립틀릿 비적용
        # OUR_REGION_TEL도 동일한 이유로 SIGUN 스크립틀릿 미사용

        query.setWhere(where_set_array)

        # 쿼리셋 구성 및 실행 — setOrderby는 예제 코드 순서대로 addQuery 이후에 호출
        queryset = jpkg_query.QuerySet(1)
        queryset.addQuery(query)
        order_set_array = [jpkg_query.OrderBySet(False, "WEIGHT", jpype.JByte(97))]
        query.setOrderby(order_set_array)
        ret = command.request(queryset)

        if ret < 0:
            logger.error(f"[Mariner/welfare] 요청 오류: {ret}")
            raise RAGServiceError(f"Mariner API 반환 코드: {ret}")

        resultSet = command.getResultSet()
        if resultSet is None:
            logger.warning("[Mariner/welfare] 서버에서 결과를 받지 못했습니다.")
            return []

        # 결과 추출
        result = resultSet.getResult(0)
        result_size = result.getRealSize()
        doc_list = []
        target_siguns = {
            normalize_sigun(str(sigun).strip())
            for sigun in (sigun_filters or [])
            if str(sigun).strip()
        }
        logger.info(f"[Mariner/welfare] raw 결과: {result_size}개 (필터 전), 키워드: {keyword[:50]}, sigun_filters={list(target_siguns) if target_siguns else None}")

        excluded_count = 0
        raw_id_samples: List[str] = []
        removed_ids: List[str] = []
        for i in range(result_size):
            try:
                raw_weight = result.getResult(i, field_indexes["WEIGHT"])
                weight_val = float(str(raw_weight)) * 0.0001
            except (ValueError, TypeError):
                weight_val = 0.0

            # if weight_val < 0.5:
            #     continue

            doc = {field_name: str(result.getResult(i, idx) or "") for field_name, idx in field_indexes.items()}
            doc["WEIGHT"] = str(weight_val)

            # WELFARE_CENTER 필드 매핑
            doc["CHUNK_ID"] = doc.get("ID", "")
            if doc["CHUNK_ID"] and len(raw_id_samples) < 10:
                raw_id_samples.append(doc["CHUNK_ID"])
            if excluded_chunk_set and doc["CHUNK_ID"] in excluded_chunk_set:
                excluded_count += 1
                if len(removed_ids) < 20:
                    removed_ids.append(doc["CHUNK_ID"])
                continue
            doc["NAME"] = str(doc.get("FACILITY_NAME", "") or "").strip()

            # CHUNK_PATH: 참조문서 스니펫에 표시될 모든 시설 정보
            _snippet_parts = []
            for _field, _label in [
                ("FACILITY_TYPE",     "시설유형"),
                ("FACILITY_CATEGORY", "분류"),
                ("ADDRESS",           "주소"),
                ("TEL",               "전화"),
                ("HOMEPAGE",          "홈페이지"),
            ]:
                _val = str(doc.get(_field, "") or "").strip()
                if _val:
                    _snippet_parts.append(f"{_label}: {_val}")
            doc["CHUNK_PATH"] = "\n".join(_snippet_parts)

            # Python 후처리: SIGUN 필터 (안전망)
            if target_siguns:
                doc_sigun = str(doc.get("SIGUN", "") or "").strip()
                doc_address = str(doc.get("ADDRESS", "") or "").strip()
                sigun_matched = False
                for ts in target_siguns:
                    if ts == "경상남도":
                        sigun_matched = True
                        break
                    short = ts.split(" ", 1)[1] if " " in ts else ts
                    if doc_sigun == ts or doc_sigun == short or short in doc_address:
                        sigun_matched = True
                        break
                if not sigun_matched:
                    continue

            # Python 후처리: FACILITY_TYPE 필터
            if facility_type_filter:
                doc_facility_type = str(doc.get("FACILITY_TYPE", "") or "").strip()
                if doc_facility_type != facility_type_filter:
                    continue

            doc_list.append(doc)

        if excluded_chunk_set:
            logger.info(f"[MoreResults][Mariner/welfare] CHUNK_ID 1차 제외: {excluded_count}개")
            logger.info(
                "[MoreResults][Mariner/welfare] 실제 제외 ID 샘플=%s | raw ID 샘플=%s",
                removed_ids[:10],
                raw_id_samples,
            )

        t2 = time.monotonic()
        logger.info(f"[Mariner/welfare] 검색 시간: {t2 - t1:.3f}초, 키워드: {keyword[:50]}, 결과: {len(doc_list)}개")
        for i, doc in enumerate(doc_list, 1):
            logger.debug(f"[Mariner/welfare] #{i} ID={doc.get('CHUNK_ID', '?')}, NAME={doc.get('NAME', '?')}, WEIGHT={doc.get('WEIGHT', '?')}")

        # Mariner 결과가 없으면 CSV fallback
        if not doc_list:
            logger.info("[Mariner/welfare] 결과 없음 → CSV fallback")
            from mariner_v2.csv_welfare_center import search_welfare_center_from_csv
            doc_list = search_welfare_center_from_csv(keyword, sigun_filters, facility_type_filter)

        return doc_list

    except RAGServiceError:
        raise
    except Exception as e:
        logger.error(f"[Mariner/welfare] 예상치 못한 오류: {e}", exc_info=True)
        logger.info("[Mariner/welfare] Mariner 오류 → CSV fallback 시도")
        try:
            from mariner_v2.csv_welfare_center import search_welfare_center_from_csv
            return search_welfare_center_from_csv(keyword, sigun_filters, facility_type_filter)
        except Exception as csv_e:
            logger.error(f"[Mariner/welfare] CSV fallback도 실패: {csv_e}")
            raise RAGServiceError(f"복지시설 검색 중 오류: {str(e)}") from e
