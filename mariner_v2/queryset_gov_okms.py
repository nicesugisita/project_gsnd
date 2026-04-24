"""
Mariner 쿼리셋 — GOV_OKMS_V1 단일 검색

정부24 공공서비스 컬렉션(GOV_OKMS_V1)을 QuerySet(1)으로 검색합니다.
TEST_OKMS_V4 듀얼 검색과 달리 단일 쿼리(벡터+키워드 혼합)로 동작합니다.
SIGUN·YEAR 필터 없음. LIFE_CYCLE 스크립틀릿 선택 적용.
"""

import logging
import time
from typing import Dict, Any, List, Optional

import jpype
from jpype import JString

from core.config import Config
from core.exceptions import RAGServiceError
from mariner_v2.jvm_manager import ensure_jvm_thread

logger = logging.getLogger(__name__)

# GOV_OKMS_V1 컬렉션의 LIFE_CYCLE 필드값은 파이프라인 표준값과 다를 수 있음
# 예: 파이프라인 "노인" → GOV_OKMS_V1 "노년"
_LIFECYCLE_MAP: Dict[str, str] = {
    "노인": "노년",
}


def _map_lifecycle_for_gov_okms(lifecycle: Optional[str]) -> Optional[str]:
    """파이프라인 lifecycle 값을 GOV_OKMS_V1 컬렉션 LIFE_CYCLE 필드값으로 변환"""
    if not lifecycle:
        return lifecycle
    return _LIFECYCLE_MAP.get(lifecycle, lifecycle)


def _build_gov_okms_document_name(doc: Dict[str, Any]) -> str:
    """GOV_OKMS 문서의 UI 표시용 이름 (SERVICE_NAME 우선, 없으면 RESPONSIBLE_MINISTRY)"""
    name = str(doc.get("SERVICE_NAME", "") or "").strip()
    if name:
        return name
    return str(doc.get("RESPONSIBLE_MINISTRY", "") or doc.get("CHUNK_ID", "") or "문서")


def query_gov_okms_documents(
    search_string: str,
    collection: str = None,
    lifecycle_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    GOV_OKMS_V1 단일 검색 (QuerySet(1))

    Args:
        search_string:    검색어 (확장쿼리 또는 트리플쿼리)
        collection:       컬렉션명 (None → Config.RAG_GOV_OKMS_COLLECTION)
        lifecycle_filter: 생애주기 필터 (예: "영유아"), None이면 미적용

    Returns:
        docs: List[Dict] — CHUNK_ID·NAME·CONTENT·WEIGHT 정규화 필드 포함
    """
    t1 = time.monotonic()

    if not Config.RAG_ENABLED:
        logger.warning("[GOV_OKMS] RAG가 비활성화되어 있습니다.")
        return []

    if collection is None:
        collection = Config.RAG_GOV_OKMS_COLLECTION

    mapped_lifecycle = _map_lifecycle_for_gov_okms(lifecycle_filter)
    if mapped_lifecycle != lifecycle_filter:
        logger.debug(f"[GOV_OKMS] lifecycle 매핑: '{lifecycle_filter}' → '{mapped_lifecycle}'")

    try:
        ensure_jvm_thread()

        jpkg_cmd = jpype.JPackage("com.diquest.ir5.client.command")
        command = jpkg_cmd.CommandSearchRequest(Config.MARINER_IP, int(Config.MARINER_PORT))
        command.setProps(Config.MARINER_IP, int(Config.MARINER_PORT), Config.MARINER_TIMEOUT, 100, 100)

        jpkg_query = jpype.JPackage("com.diquest.ir5.common.msg.protocol.query")

        # SELECT 13 fields (MarinerQuerySetExampleCode_GOV_OKMS_V1.py 기준)
        num = 16
        select_field_names = [
            "SERVICE_ID",           # 0
            "SERVICE_NAME",         # 1
            "RESPONSIBLE_MINISTRY", # 2
            "TARGET_DETAILS",       # 3
            "SELECTION_CRITERIA",   # 4
            "BENEFIT_DETAILS",      # 5
            "CONTACT_POINT",        # 6
            "SERVICE_TYPE",         # 7
            "LIFE_CYCLE",           # 8
            "INTEREST_TOPICS",      # 9
            "WEIGHT",               # 10
            "SERVICE_DESCRIPTION",  # 11
            "APPLICATION_PERIOD",   # 12
        ]
        select_set_array = [jpkg_query.SelectSet(JString(f), num, 0) for f in select_field_names]
        field_indexes = {f: i for i, f in enumerate(select_field_names)}

        query = jpkg_query.Query("", "")
        ks = JString(search_string)

        _TOP_N       = 5   # 반환 문서 수
        _THRESHOLD   = 0.2  # 샘플 코드 기준값 (Config.MARINER_THRESHOLD=0.5보다 낮게 유지)
        _RESULT_SIZE = 50   # 벡터 검색 풀 크기 (샘플 코드 기준값)

        query.setResult(0, _TOP_N - 1)
        query.setFrom(collection)
        query.setSearch(True)
        query.setDebug(True)
        query.setPrintQuery(True)
        query.setLoggable(True)
        query.setValue("VS_THRESHOLD",   str(_THRESHOLD))
        query.setValue("VS_RESULT_SIZE", str(_RESULT_SIZE))
        query.setSelect(select_set_array)
        query.setOrderby([jpkg_query.OrderBySet(True, "WEIGHT", jpype.JByte(97))])

        # WHERE: 4-field OR (sample code 기준)
        where_set_array = [
            jpkg_query.WhereSet(9),                                          # OR (
            jpkg_query.WhereSet("SERVICE_NAME_KO", 2,  ks, 0.7),            #   서비스명 키워드
            jpkg_query.WhereSet(6),                                          #   OR
            jpkg_query.WhereSet("TEXT_CHUNK_KO",   2,  ks, 0.7),            #   텍스트 키워드
            jpkg_query.WhereSet(6),                                          #   OR
            jpkg_query.WhereSet("SERVICE_NAME_MI", 2,  ks),                 #   서비스명 벡터
            jpkg_query.WhereSet(6),                                          #   OR
            jpkg_query.WhereSet("TEXT_CHUNK_MI",   96, ks, 0.3),            #   텍스트 벡터
            jpkg_query.WhereSet(10),                                         # )
        ]

        if mapped_lifecycle:
            where_set_array += [
                jpkg_query.WhereSet(5),
                jpkg_query.WhereSet("LIFE_CYCLE", 34, mapped_lifecycle, 0),
            ]

        query.setWhere(where_set_array)

        queryset = jpkg_query.QuerySet(1)
        queryset.addQuery(query)

        logger.info(
            f"[Mariner/GOV_OKMS] 검색: '{search_string[:60]}'"
            f"  lifecycle={mapped_lifecycle or '없음'} (원본: {lifecycle_filter or '없음'})"
            f"  collection={collection}"
        )

        ret = command.request(queryset)
        if ret < 0:
            logger.error(f"[Mariner/GOV_OKMS] 요청 오류: {ret}")
            raise RAGServiceError(f"Mariner API 반환 코드: {ret}")

        resultSet = command.getResultSet()
        if resultSet is None:
            logger.warning("[Mariner/GOV_OKMS] 서버에서 결과를 받지 못했습니다.")
            return []

        result = resultSet.getResult(0)
        result_size = result.getRealSize()
        logger.info(f"[Mariner/GOV_OKMS] raw 결과: {result_size}개")

        docs: List[Dict[str, Any]] = []
        for i in range(result_size):
            try:
                raw_weight = result.getResult(i, field_indexes["WEIGHT"])
                weight_val = float(str(raw_weight)) * 0.0001
            except (ValueError, TypeError):
                weight_val = 0.0

            doc = {f: str(result.getResult(i, idx) or "") for f, idx in field_indexes.items()}
            doc["WEIGHT"] = str(weight_val)

            # 파이프라인 공통 정규화 필드
            doc["CHUNK_ID"]      = doc.get("SERVICE_ID", "")
            doc["NAME"]          = _build_gov_okms_document_name(doc)
            doc["BUSINESS_NAME"] = doc.get("SERVICE_NAME", "")
            doc["ORG_NM"]        = doc.get("RESPONSIBLE_MINISTRY", "")
            # SERVICE_DESCRIPTION + 지원대상/지원내용/문의처/생애주기를 하나의 content로 합산
            _content_parts = []
            if doc.get("SERVICE_DESCRIPTION", "").strip():
                _content_parts.append(doc["SERVICE_DESCRIPTION"].strip())
            for _field, _label in [
                ("TARGET_DETAILS",   "지원대상"),
                ("BENEFIT_DETAILS",  "지원내용"),
                ("CONTACT_POINT",    "문의처"),
                ("LIFE_CYCLE",       "생애주기"),
            ]:
                _val = doc.get(_field, "").strip()
                if _val:
                    # GOV_OKMS "노년" → 프롬프트 표준 "노인" 으로 표시
                    if _field == "LIFE_CYCLE":
                        _val = {v: k for k, v in _LIFECYCLE_MAP.items()}.get(_val, _val)
                    _content_parts.append(f"{_label}: {_val}")
            _combined = "\n".join(_content_parts)
            doc["CONTENT"]       = _combined
            doc["CHUNK_PATH"]    = _combined
            # GOV_OKMS_V1에는 SIGUN·YEAR 없음 — 로깅·정렬 호환용 빈 문자열
            doc.setdefault("SIGUN", "")
            doc.setdefault("YEAR", "")

            docs.append(doc)

        logger.info(
            f"[Mariner/GOV_OKMS] {time.monotonic() - t1:.3f}초, 결과: {len(docs)}개 "
            f"lifecycle={mapped_lifecycle or '없음'}"
        )
        return docs

    except RAGServiceError:
        raise
    except Exception as e:
        logger.error(f"[Mariner/GOV_OKMS] 예상치 못한 오류: {e}", exc_info=True)
        raise RAGServiceError(f"GOV_OKMS 검색 오류: {str(e)}") from e
