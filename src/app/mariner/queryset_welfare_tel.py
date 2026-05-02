"""
Mariner 쿼리셋 — GSND_OUR_REGION_TEL 전용

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
    OP_AND,
    OP_HASANY,
    MARINER_WEIGHT_HIGH,
)
from app.core.exceptions import RAGServiceError
from app.mariner.jvm_manager import ensure_jvm_thread

logger = logging.getLogger(__name__)

# Mariner setResult/VS_RESULT_SIZE 상한: 엔진·컬렉션 실물 개수보다 크면 전부 반환
_OUR_REGION_TEL_UNBOUND_FETCH = 500_000


def query_welfare_tel_documents(
    keyword: str,
    sigun_filters: Optional[List[str]] = None,
    eupmyeondong_filters: Optional[List[str]] = None,
    max_results: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    GSND_OUR_REGION_TEL 컬렉션 전용 Mariner 검색

    예제 검색식(MarinerQuerySetExampleCode_OUR_REGION_TEL.py) 기반:
    - WHERE: CENTER OP_HASANY OR ADDRESS OP_HASANY (키워드), 이후 AND로 SIGUN·EUPMYEONDONG 스크립틀릿(op 1) 가능
    - SIGUN은 키워드 OR절에 넣지 않음(공통 시군명 과매칭 방지). sigun_filters가 있을 때만 AND 부분일치(op 1).
      창원은 DB에 「경상남도 창원시 의창구·성산구·마산합포구·마산회원구·진해구」 형태로 들어 있으므로,
      세션 값이 「경상남도 창원시」이면 해당 접두 부분일치로 위 구 행까지 한 번에 한정된다.
    - EUPMYEONDONG 스크립틀릿: op 1 (부분형 "동읍")
    - OrderBy: WEIGHT 수치 내림차순 (JByte(97))

    Args:
        keyword: 검색 키워드 (센터명 또는 주소)
        sigun_filters: SIGUN 필드 필터 목록 (예: 전체 창원은 「경상남도 창원시」, 진해만은 「경상남도 창원시 진해구」)
        eupmyeondong_filters: EUPMYEONDONG 필드 필터 목록 (예: ["동읍"])
        max_results: None이면 Config.MARINER_MAX_RESULTS, 양수면 해당 개수, 음수면
            컬렉션에서 가져올 수 있는 범위까지(내부 상한 `_OUR_REGION_TEL_UNBOUND_FETCH`).

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

    try:
        timeout = Config.MARINER_TIMEOUT
        threshold = Config.MARINER_THRESHOLD
        if max_results is not None:
            _mr = int(max_results)
            if _mr < 0:
                max_top_n = _OUR_REGION_TEL_UNBOUND_FETCH
            else:
                max_top_n = max(1, _mr)
        else:
            max_top_n = max(1, int(Config.MARINER_MAX_RESULTS))

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

        # WHERE: 키워드는 CENTER·ADDRESS에만 (SIGUN BM25 제외)
        # — SIGUN에 HASANY를 걸면 "경상남도 창원시" 등 공통 토큰으로 타 구·타 센터까지 과다 매칭되기 쉬움.
        # 지역 한정은 아래 sigun_filters 스크립틀릿 AND + EUPMYEONDONG AND로 수행.
        where_set_array = [
            jpkg_query.WhereSet(OP_BRACE_OPEN),
            jpkg_query.WhereSet("CENTER", OP_HASANY, keyword_string, MARINER_WEIGHT_HIGH),
            jpkg_query.WhereSet(OP_OR),
            jpkg_query.WhereSet("ADDRESS", OP_HASANY, keyword_string, MARINER_WEIGHT_HIGH),
            jpkg_query.WhereSet(OP_BRACE_CLOSE),
        ]

        # SIGUN 스크립틀릿 (op 1 부분형): 세션·전처리 시군구
        # 창원: DB 값은 「경상남도 창원시 의창구」「… 성산구」「… 마산합포구」「… 마산회원구」「… 진해구」
        # 필터 「경상남도 창원시」 → 위 전 구 매칭, 「경상남도 창원시 진해구」 → 진해 행만
        sigun_values = [s.strip() for s in (sigun_filters or []) if s and str(s).strip()]

    

        # if sigun_values:
        #     where_set_array.append(jpkg_query.WhereSet(OP_AND))
        #     if len(sigun_values) == 1:
        #         where_set_array.append(
        #             jpkg_query.FilterSet("SIGUN", 3, JString(sigun_values[0]), 0),
        #         )
        #     else:
        #         where_set_array.append(jpkg_query.FilterSet(OP_BRACE_OPEN))
        #         for idx, sv in enumerate(sigun_values):
        #             if idx > 0:
        #                 where_set_array.append(jpkg_query.FilterSet(OP_OR))
        #             where_set_array.append(jpkg_query.FilterSet("SIGUN", 1, JString(sv), 0))
        #         where_set_array.append(jpkg_query.FilterSet(OP_BRACE_CLOSE))

        # EUPMYEONDONG 스크립틀릿 (op 1, 부분형 "동읍")
        eupmyeondong_values = [e for e in (eupmyeondong_filters or []) if e]
        if eupmyeondong_values:
            if len(eupmyeondong_values) == 1:
                where_set_array += [
                    jpkg_query.WhereSet(OP_AND),
                    jpkg_query.WhereSet("EUPMYEONDONG", 1, eupmyeondong_values[0], 0),
                ]
            else:
                where_set_array.append(jpkg_query.WhereSet(OP_AND))
                where_set_array.append(jpkg_query.WhereSet(OP_BRACE_OPEN))   # OR (
                for idx, ev in enumerate(eupmyeondong_values):
                    if idx > 0:
                        where_set_array.append(jpkg_query.WhereSet(OP_OR))   # OR
                    where_set_array.append(jpkg_query.WhereSet("EUPMYEONDONG", 1, ev, 0))
                where_set_array.append(jpkg_query.WhereSet(OP_BRACE_CLOSE))  # )

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

        for i in range(result_size):
            try:
                raw_weight = result.getResult(i, field_indexes["WEIGHT"])
                weight_val = float(str(raw_weight)) * 0.0001
            except (ValueError, TypeError):
                weight_val = 0.0

            doc = {field_name: str(result.getResult(i, idx) or "") for field_name, idx in field_indexes.items()}
            doc["WEIGHT"] = str(weight_val)
            doc["CHUNK_ID"] = doc.get("ID", "")
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

        return doc_list

    except RAGServiceError:
        raise
    except Exception as e:
        logger.error(f"[Mariner/our_region_tel] 예상치 못한 오류: {e}", exc_info=True)
        raise RAGServiceError(f"지역 연락처 검색 중 오류: {str(e)}") from e
