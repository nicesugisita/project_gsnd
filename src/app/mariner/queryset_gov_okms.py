"""
Mariner 쿼리셋 — GOV_OKMS_V1 단일 검색

"""

import logging
import time
from typing import Dict, Any, List, Optional

import jpype
from jpype import JString

from app.core.config import Config
from app.core.constants import (
    OP_BRACE_OPEN,
    OP_OR,
    OP_BRACE_CLOSE,
    OP_AND,
    OP_NOT,
    OP_INT_SUMMATION,
    OP_HASANY,
)
from app.core.exceptions import RAGServiceError
from app.mariner.jvm_manager import ensure_jvm_thread

logger = logging.getLogger(__name__)

# 검색단에 NOT pair 로 적용할 제외 ID 의 상한.
# 초과분은 호출부의 filter_excluded_docs 가 post-filter 로 처리.
# (NOT pair 가 많아지면 Mariner 쿼리 트리가 커져 -60004 타임아웃 발생)
_MARINER_SEARCH_EXCLUSION_CAP = 15

# GOV_OKMS_V1 컬렉션의 LIFE_CYCLE 필드값은 파이프라인 표준값과 다를 수 있음
# 예: 파이프라인 "노인" → GOV_OKMS_V1 "노년"
_LIFECYCLE_MAP: Dict[str, str] = {
    "노인": "노년",
}

# 가구상황 동의어 토큰 캡 (queryset_okms 와 동일 정책)
_HSHD_SYNONYMS_MAX_TOKENS = 5


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
    sigun_filters: Optional[List[str]] = None,
    excluded_chunk_ids: Optional[List[str]] = None,
    excluded_business_keywords: Optional[List[str]] = None,
    hshd_sttn_filter: Optional[str] = None,
    hshd_sttn_synonyms: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    GOV_OKMS_V1 단일 검색 (QuerySet(1))

    Args:
        search_string:      검색어 (확장쿼리 또는 트리플쿼리)
        collection:         컬렉션명 (None → Config.RAG_GOV_OKMS_COLLECTION)
        lifecycle_filter:   생애주기 필터 (예: "영유아"), None이면 미적용
        sigun_filters:      시군 필터 목록(표시용 SIGUN 기본값 보정에만 사용)
        hshd_sttn_filter:   가구상황 정규화값 (예: "저소득"), HOUSE_SITUATION 필드에 op=34 적용
        hshd_sttn_synonyms: 가구상황 동의어 토큰 (SERVICE_NAME_KO/TEXT_CHUNK_KO 부스팅용)

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

    # queryset_okms.py와 동일 패턴: "경상남도" 제외한 실제 시군 목록
    sigun_scriptlet_values = [
        s for s in (sigun_filters or [])
        if s and s != "경상남도"
    ]

    excluded_chunk_set = {
        str(chunk_id).strip()
        for chunk_id in (excluded_chunk_ids or [])
        if str(chunk_id).strip()
    }
    if excluded_chunk_set:
        excluded_values = sorted(excluded_chunk_set)
        logger.info(
            "[MoreResults][Mariner/GOV_OKMS] 제외 입력 수=%d | 샘플=%s",
            len(excluded_values),
            excluded_values[:10],
        )

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
            jpkg_query.WhereSet(OP_BRACE_OPEN),                              # OR (
            jpkg_query.WhereSet("SERVICE_NAME_KO", 2,  ks, 0.7),            #   서비스명 키워드
            jpkg_query.WhereSet(OP_OR),                             #   OR
            jpkg_query.WhereSet("TEXT_CHUNK_KO",   2,  ks, 0.3),            #   텍스트 키워드
            jpkg_query.WhereSet(OP_OR),                             #   OR
            jpkg_query.WhereSet("SERVICE_NAME_MI", 2,  ks, 0.3),                 #   서비스명 벡터
            jpkg_query.WhereSet(OP_OR),                             #   OR
            jpkg_query.WhereSet("TEXT_CHUNK_MI",   96, ks, 0.3),            #   텍스트 벡터
        ]

        # 가구상황 동의어 부스팅 (SERVICE_NAME_KO / TEXT_CHUNK_KO 만)
        # - 메인 4-필드 OR 괄호 안에 동일 레벨로 OR-append → 결과 broaden
        # - search_string 원본 손대지 않음 → 타 검색 흐름 영향 없음
        # - 토큰 캡으로 트리 폭증 방지
        _syn_tokens = [
            str(t).strip()
            for t in (hshd_sttn_synonyms or [])
            if t and str(t).strip()
        ]
        if _syn_tokens:
            _syn_str = " ".join(_syn_tokens[:_HSHD_SYNONYMS_MAX_TOKENS])
            _syn_ks = JString(_syn_str)
            where_set_array += [
                jpkg_query.WhereSet(OP_OR),
                jpkg_query.WhereSet("SERVICE_NAME_KO", 2, _syn_ks, 0.3),
                jpkg_query.WhereSet(OP_OR),
                jpkg_query.WhereSet("TEXT_CHUNK_KO",   2, _syn_ks, 0.3),
            ]
            logger.debug(
                "[Mariner/GOV_OKMS] HSHD 동의어 부스팅 적용: %s (적용 토큰=%d/%d)",
                _syn_str,
                min(len(_syn_tokens), _HSHD_SYNONYMS_MAX_TOKENS),
                len(_syn_tokens),
            )

        where_set_array.append(jpkg_query.WhereSet(OP_BRACE_CLOSE))          # )

        if mapped_lifecycle:
            where_set_array += [
                jpkg_query.WhereSet(OP_AND),
                jpkg_query.WhereSet("LIFE_CYCLE", 34, mapped_lifecycle, 0),
            ]

        # HOUSE_SITUATION(가구상황) 스크립틀릿 필터 — LIFE_CYCLE 동일 패턴 (op=34)
        if hshd_sttn_filter:
            where_set_array += [
                jpkg_query.WhereSet(OP_AND),
                jpkg_query.WhereSet("HOUSE_SITUATION", 34, hshd_sttn_filter, 0),
            ]

        # CHUNK_ID(SERVICE_ID) 제외 필터 (예제 패턴: NOT + EXACT 반복)
        # NOT pair 가 많아지면 Mariner 쿼리 트리가 폭증해 -60004(타임아웃) 발생.
        # 검색단은 cap 까지만 적용하고, 잔여는 호출부의 filter_excluded_docs(post-filter)가 처리한다.
        if excluded_chunk_set:
            excluded_values = sorted(excluded_chunk_set)
            _n_total = len(excluded_values)
            search_excluded_values = excluded_values[:_MARINER_SEARCH_EXCLUSION_CAP]
            _n_applied = len(search_excluded_values)
            _overflow = _n_total - _n_applied
            logger.info(
                "[MoreResults][Mariner/GOV_OKMS] 검색단 제외 IDs 총=%d 적용=%d%s 샘플=%s",
                _n_total,
                _n_applied,
                f" (cap={_MARINER_SEARCH_EXCLUSION_CAP}, 잔여 {_overflow}건 post-filter)" if _overflow > 0 else "",
                search_excluded_values[:20],
            )
            for chunk_id in search_excluded_values:
                where_set_array += [
                    jpkg_query.WhereSet(OP_NOT),
                    jpkg_query.WhereSet("SERVICE_ID", OP_INT_SUMMATION, chunk_id, 0),
                ]

        # 사용자 명시 배제 사업명(예: "의료급여 외") 추출값을 SERVICE_NAME_KO 토큰
        # 정확 부정 조건으로 추가. extract_excluded_services 가 추출한 키워드가
        # 사업명에 포함된 문서를 검색 단계에서 사전 제외.
        # (OKMS는 BUSINESS_NAME_KO, GOV_OKMS는 SERVICE_NAME_KO가 사업명 토큰 색인 필드)
        if excluded_business_keywords:
            _biz_excluded = [
                str(k).strip()
                for k in excluded_business_keywords
                if str(k or "").strip()
            ]
            if _biz_excluded:
                logger.info(
                    "[Mariner/GOV_OKMS] SERVICE_NAME_KO 배제 키워드 적용: %s",
                    _biz_excluded,
                )
                for kw in _biz_excluded:
                    where_set_array += [
                        jpkg_query.WhereSet(OP_NOT),
                        jpkg_query.WhereSet("SERVICE_NAME_KO", OP_HASANY, kw, 0),
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
        excluded_count = 0
        raw_id_samples: List[str] = []
        removed_id_samples: List[str] = []
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
            if doc["CHUNK_ID"] and len(raw_id_samples) < 10:
                raw_id_samples.append(doc["CHUNK_ID"])
            if excluded_chunk_set and doc["CHUNK_ID"] in excluded_chunk_set:
                excluded_count += 1
                if len(removed_id_samples) < 20:
                    removed_id_samples.append(doc["CHUNK_ID"])
                continue
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
            # GOV_OKMS_V1에는 SIGUN 필드가 없어 queryset_okms와 동일하게 보정한다.
            doc.setdefault("SIGUN", sigun_scriptlet_values[0] if sigun_scriptlet_values else "")
            doc.setdefault("YEAR", "")

            docs.append(doc)

        if excluded_chunk_set:
            logger.info(f"[MoreResults][Mariner/GOV_OKMS] CHUNK_ID 1차 제외: {excluded_count}개")
            logger.info("[MoreResults][Mariner/GOV_OKMS] raw ID 샘플=%s", raw_id_samples)
            logger.info("[MoreResults][Mariner/GOV_OKMS] 실제 제외 ID 샘플=%s", removed_id_samples[:10])

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
