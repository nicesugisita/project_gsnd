"""
Mariner 쿼리셋 — GSND_OUR_REGION_TEL 전용

우리 지역 센터/기관 연락처(센터명, 읍면동, 전화번호, 주소) 검색에 사용됩니다.
예제 검색식(MarinerQuerySetExampleCode_OUR_REGION_TEL.py)을 기반으로
CENTER/ADDRESS 2-field OR 검색식과 SIGUN/EUPMYEONDONG 스크립틀릿을 적용합니다.
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
    MARINER_WS_EXACT,
    MARINER_WEIGHT_HIGH,
)
from core.exceptions import RAGServiceError
from mariner_v2.jvm_manager import ensure_jvm_thread

logger = logging.getLogger(__name__)


def query_welfare_tel_documents(
    keyword: str,
    sigun_filters: Optional[List[str]] = None,
    eupmyeondong_filters: Optional[List[str]] = None,
    excluded_chunk_ids: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    GSND_OUR_REGION_TEL 컬렉션 전용 Mariner 검색

    예제 검색식(MarinerQuerySetExampleCode_OUR_REGION_TEL.py) 기반:
    - WHERE: CENTER (op=2 BM25, 0.7) OR ADDRESS (op=1, 0.7)
    - SIGUN 스크립틀릿: op 1 (전체형 "경상남도 창원시")
    - EUPMYEONDONG 스크립틀릿: op 1 (부분형 "동읍")
    - OrderBy: WEIGHT 수치 내림차순 (JByte(97))

    Args:
        keyword: 검색 키워드 (센터명 또는 주소)
        sigun_filters: SIGUN 필드 필터 목록 (예: ["경상남도 창원시"])
        eupmyeondong_filters: EUPMYEONDONG 필드 필터 목록 (예: ["동읍"])

    Returns:
        검색된 지역 연락처 문서 목록

    Raises:
        RAGServiceError: JPype 또는 Mariner 호출 실패 시
    """
    t1 = time.monotonic()

    if not Config.RAG_ENABLED:
        logger.warning("RAG가 비활성화되어 있습니다.")
        return []

    collection = Config.RAG_WELFARE_TEL_COLLECTION

    excluded_chunk_set = {
        str(chunk_id).strip()
        for chunk_id in (excluded_chunk_ids or [])
        if str(chunk_id).strip()
    }
    if excluded_chunk_set:
        excluded_values = sorted(excluded_chunk_set)
        logger.info(
            "[MoreResults][Mariner/our_region_tel] 제외 입력 수=%d | 샘플=%s",
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
        logger.info(f"[query_our_region_tel] keyword={keyword!r}, sigun={sigun_filters}, eupmyeondong={eupmyeondong_filters}")
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

        # SELECT 필드 (GSND_OUR_REGION_TEL 전용)
        num = MARINER_SELECT_FIELD_NUM
        select_field_names = [
            "ID", "SIGUN", "CENTER", "EUPMYEONDONG", "TEL", "ADDRESS", "WEIGHT",
        ]

        select_set_array = [
            jpkg_query.SelectSet(JString(field_name), num, 0)
            for field_name in select_field_names
        ]
        field_indexes = {field_name: idx for idx, field_name in enumerate(select_field_names)}
        query.setSelect(select_set_array)

        # 정렬: WEIGHT 수치 내림차순 (JByte(97))
        order_set_array = [jpkg_query.OrderBySet(False, "WEIGHT", jpype.JByte(97))]
        query.setOrderby(order_set_array)

        # WHERE: CENTER OR ADDRESS 2-field 검색식
        where_set_array = [
            jpkg_query.WhereSet(MARINER_WS_OR),                                        # OR (
            jpkg_query.WhereSet("CENTER",  70, keyword_string, MARINER_WEIGHT_HIGH),        # CENTER BM25
            jpkg_query.WhereSet(MARINER_WS_AND),                                        #   OR
            jpkg_query.WhereSet("ADDRESS", 30, keyword_string, MARINER_WEIGHT_HIGH),        # ADDRESS
            jpkg_query.WhereSet(MARINER_WS_END),                                       # )
        ]

        # CHUNK_ID(ID) 제외 필터 (예제 패턴: NOT + EXACT 반복)
        if excluded_chunk_set:
            excluded_values = sorted(excluded_chunk_set)
            _n = len(excluded_values)
            _sample = excluded_values[:20]
            logger.info(
                "[MoreResults][Mariner/our_region_tel] 검색단 제외 IDs(%d): %s%s",
                _n,
                _sample,
                "..." if _n > 20 else "",
            )
            for chunk_id in excluded_values:
                where_set_array += [
                    jpkg_query.WhereSet(MARINER_WS_NOT),
                    jpkg_query.WhereSet("ID", MARINER_WS_EXACT, chunk_id, 0),
                ]

        # SIGUN 스크립틀릿 미적용
        # OUR_REGION_TEL은 창원시 구(區) 단위("경상남도 의창구" 등)로 SIGUN을 저장하여
        # 정규화된 시 단위("경상남도 창원시")와 포맷 불일치가 발생합니다.
        # EUPMYEONDONG + 키워드 검색으로 지역 필터링이 충분합니다.

        # EUPMYEONDONG 스크립틀릿 (op 1, 부분형 "동읍")
        eupmyeondong_values = [e for e in (eupmyeondong_filters or []) if e]
        if eupmyeondong_values:
            if len(eupmyeondong_values) == 1:
                where_set_array += [
                    jpkg_query.WhereSet(MARINER_WS_FILTER),
                    jpkg_query.WhereSet("EUPMYEONDONG", 1, eupmyeondong_values[0], 0),
                ]
            else:
                where_set_array.append(jpkg_query.WhereSet(MARINER_WS_FILTER))
                where_set_array.append(jpkg_query.WhereSet(MARINER_WS_OR))   # OR (
                for idx, ev in enumerate(eupmyeondong_values):
                    if idx > 0:
                        where_set_array.append(jpkg_query.WhereSet(MARINER_WS_AND))   # OR
                    where_set_array.append(jpkg_query.WhereSet("EUPMYEONDONG", 1, ev, 0))
                where_set_array.append(jpkg_query.WhereSet(MARINER_WS_END))  # )

        query.setWhere(where_set_array)

        queryset = jpkg_query.QuerySet(1)
        queryset.addQuery(query)
        ret = command.request(queryset)

        if ret < 0:
            logger.error(f"[Mariner/our_region_tel] 요청 오류: {ret}")
            raise RAGServiceError(f"Mariner API 반환 코드: {ret}")

        resultSet = command.getResultSet()
        if resultSet is None:
            logger.warning("[Mariner/our_region_tel] 서버에서 결과를 받지 못했습니다.")
            return []

        result = resultSet.getResult(0)
        result_size = result.getRealSize()
        doc_list = []
        logger.info(f"[Mariner/our_region_tel] raw 결과: {result_size}개, 키워드: {keyword[:50]}")

        excluded_count = 0
        raw_id_samples: List[str] = []
        removed_ids: List[str] = []
        for i in range(result_size):
            try:
                raw_weight = result.getResult(i, field_indexes["WEIGHT"])
                weight_val = float(str(raw_weight)) * 0.0001
            except (ValueError, TypeError):
                weight_val = 0.0

            doc = {field_name: str(result.getResult(i, idx) or "") for field_name, idx in field_indexes.items()}
            doc["WEIGHT"] = str(weight_val)
            doc["CHUNK_ID"] = doc.get("ID", "")
            if doc["CHUNK_ID"] and len(raw_id_samples) < 10:
                raw_id_samples.append(doc["CHUNK_ID"])
            if excluded_chunk_set and doc["CHUNK_ID"] in excluded_chunk_set:
                excluded_count += 1
                if len(removed_ids) < 20:
                    removed_ids.append(doc["CHUNK_ID"])
                continue
            doc["_source"] = "our_region_tel"

            # CHUNK_PATH: 핵심 필드 스니펫
            snippet_parts = []
            for field, label in [
                ("CENTER",       "센터명"),
                ("EUPMYEONDONG", "읍면동"),
                ("TEL",          "연락처"),
                ("ADDRESS",      "주소"),
            ]:
                val = str(doc.get(field, "") or "").strip()
                if val:
                    snippet_parts.append(f"{label}: {val}")
            doc["CHUNK_PATH"] = "\n".join(snippet_parts)

            doc_list.append(doc)

        if excluded_chunk_set:
            logger.info(f"[MoreResults][Mariner/our_region_tel] CHUNK_ID 1차 제외: {excluded_count}개")
            logger.info(
                "[MoreResults][Mariner/our_region_tel] 실제 제외 ID 샘플=%s | raw ID 샘플=%s",
                removed_ids[:10],
                raw_id_samples,
            )

        t2 = time.monotonic()
        logger.info(
            f"[Mariner/our_region_tel] 검색 시간: {t2 - t1:.3f}초, "
            f"키워드: {keyword[:50]}, 결과: {len(doc_list)}개"
        )
        for i, doc in enumerate(doc_list, 1):
            logger.debug(
                f"[Mariner/our_region_tel] #{i} ID={doc.get('CHUNK_ID', '?')}, "
                f"CENTER={doc.get('CENTER', '?')}, SIGUN={doc.get('SIGUN', '?')}, "
                f"WEIGHT={doc.get('WEIGHT', '?')}"
            )

        # Mariner 결과가 없으면 CSV fallback
        if not doc_list:
            logger.info("[Mariner/our_region_tel] 결과 없음 → CSV fallback")
            from mariner_v2.csv_welfare_tel import search_welfare_tel_from_csv
            doc_list = search_welfare_tel_from_csv(keyword, sigun_filters, eupmyeondong_filters)

        return doc_list

    except RAGServiceError:
        raise
    except Exception as e:
        logger.error(f"[Mariner/our_region_tel] 예상치 못한 오류: {e}", exc_info=True)
        logger.info("[Mariner/our_region_tel] Mariner 오류 → CSV fallback 시도")
        try:
            from mariner_v2.csv_welfare_tel import search_welfare_tel_from_csv
            return search_welfare_tel_from_csv(keyword, sigun_filters, eupmyeondong_filters)
        except Exception as csv_e:
            logger.error(f"[Mariner/our_region_tel] CSV fallback도 실패: {csv_e}")
            raise RAGServiceError(f"지역 연락처 검색 중 오류: {str(e)}") from e
