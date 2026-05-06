"""
Mariner 쿼리셋 — GSND_OUR_REGION_TEL 전용

색인 추출(컬렉션 설정 기준): SIGUN·EUPMYEONDONG·TEL=필드, CENTER=한국어 명사, ADDRESS=어절.
WHERE는 (CENTER·ADDRESS의 OP_HASANY) OR (동일 필드 스크립틀릿 op 1 부분일치) OR (EUPMYEONDONG op 1)
로 형태소 미적중 시에도 기관명·주소·읍면동 문자열을 잡는다.
사용자 질의 원문(공백 정규화만 적용)을 그대로 Mariner 검색어로 사용한다.
EUPMYEONDONG 필터로 들어온 `○동`(번호 없음)은 `○1동`…`○N동` OR로 확장한다.
ID·WEIGHT는 SELECT/정렬 전용.
"""

import logging
import re
import time
from typing import Any, Dict, List, Optional, Tuple

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
    OP_HASALL,
    OP_HASANY,
    OP_RIGHT_TRUNCATION,
)
from app.core.exceptions import RAGServiceError
from app.mariner.jvm_manager import ensure_jvm_thread

logger = logging.getLogger(__name__)

# Mariner setResult/VS_RESULT_SIZE 상한: 엔진·컬렉션 실물 개수보다 크면 전부 반환
_OUR_REGION_TEL_UNBOUND_FETCH = 500_000
_SCRIPTLET_OP_PARTIAL = 1
_SCRIPTLET_WEIGHT_NONE = 0
_ORDERBY_WEIGHT_DESC = 97
_WEIGHT_SCALE = 0.0001
_WELFARE_TEL_SELECT_FIELDS: tuple[str, ...] = (
    "ID",
    "SIGUN",
    "CENTER",
    "EUPMYEONDONG",
    "TEL",
    "ADDRESS",
    "WEIGHT",
)
_WELFARE_TEL_SNIPPET_FIELDS: tuple[tuple[str, str], ...] = (
    ("CENTER", "센터명"),
    ("EUPMYEONDONG", "읍면동"),
    ("TEL", "연락처"),
    ("ADDRESS", "주소"),
)

_DONG_DIGIT_EXPAND_MAX: int = 9
def _expand_sigun_scriptlet_values(sigun_values: List[str]) -> List[str]:
    """SIGUN 필터값을 WhereSet 스크립틀릿용 OR 토큰으로 정규화한다."""
    out: List[str] = []
    seen: set[str] = set()
    for raw in sigun_values or []:
        v = str(raw).strip()
        if not v:
            continue
        variants = [v]
        parts = v.split()
        # 광역도 + 시군구 형태면 시군구 축약형도 함께 사용 (예: 경상남도 창원시 -> 창원시)
        if len(parts) >= 2 and parts[0].endswith("도"):
            variants.append(" ".join(parts[1:]).strip())
        for cand in variants:
            c = cand.strip()
            if c and c not in seen:
                seen.add(c)
                out.append(c)
    return out


_EUPMYEONDONG_FILTER_AMBIGUOUS_DONG = re.compile(r"^([가-힣]{2,10})동$")


def _eupmyeondong_filter_or_tokens(ev: str) -> List[str]:
    """
    파이프라인이 추출한 `회원동`이 색인 EUPMYEONDONG `회원1동`과 다를 때 OR로 풀어준다.
    이미 `회원1동` 형태이거나 읍·면 등은 그대로 둔다.
    """
    m = _EUPMYEONDONG_FILTER_AMBIGUOUS_DONG.fullmatch(ev.strip())
    if not m:
        return [ev]
    base = m.group(1)
    seen: set[str] = set()
    out: List[str] = []
    for tok in [ev] + [f"{base}{n}동" for n in range(1, _DONG_DIGIT_EXPAND_MAX + 1)]:
        if tok not in seen:
            seen.add(tok)
            out.append(tok)
    return out


def _expand_ambiguous_eupmyeondong_filters(values: List[str]) -> List[str]:
    """복수 입력 시 전체 OR에 쓰일 토큰 목록(중복 제거, 순서 유지)."""
    seen: set[str] = set()
    out: List[str] = []
    for raw in values or []:
        ev = str(raw).strip()
        if not ev:
            continue
        for tok in _eupmyeondong_filter_or_tokens(ev):
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out


def query_welfare_tel_documents(
    keyword: str,
    sigun_filters: Optional[List[str]] = None,
    eupmyeondong_filters: Optional[List[str]] = None,
    max_results: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    GSND_OUR_REGION_TEL 컬렉션 전용 Mariner 검색

    예제 검색식(MarinerQuerySetExampleCode_OUR_REGION_TEL.py) 기반:
    - WHERE: SIGUN 스크립틀릿(op 1) 중심 필터링.
      키워드 확장은 사용하지 않으며, 검색 대상 범위는 sigun_filters로 결정한다.
    - 원천이 시군구·읍면동 컬럼 분리면 SIGUN은 보통 「경상남도 OO시」 수준이고
      구·동만 한정할 때는 eupmyeondong_filters를 사용한다.
      `회원동`처럼 번 없는 동명 필터는 색인 `회원1동` 등과 맞추기 위해 내부에서 1…N동 OR로 확장한다.
    - EUPMYEONDONG 스크립틀릿: op 1 (부분형 "동읍")
    - OrderBy: WEIGHT 수치 내림차순 (JByte(97))

    Args:
        keyword: 검색 키워드 (센터명 또는 주소)
        sigun_filters: SIGUN(시군구) 필터. 예: 「경상남도 창원시」. 구·동만 걸 경우 eupmyeondong_filters 사용.
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
        sigun_scriptlet_values: List[str] = [
            str(s).strip() for s in (sigun_filters or []) if str(s).strip()
        ]
        if not sigun_scriptlet_values:
            logger.info("[query_our_region_tel] sigun_filters 없음 -> 검색 생략")
            return []

        eup_raw_filters = [str(e).strip() for e in (eupmyeondong_filters or []) if e and str(e).strip()]
        eup_expanded_for_where = _expand_ambiguous_eupmyeondong_filters(eup_raw_filters)
        # setSearch(True) 경로에서 SIGUN 필터와 동일 축의 검색어를 사용
        search_kw = sigun_scriptlet_values[0]
        logger.info(
            "[query_our_region_tel] keyword=%r search_keyword=%r sigun=%s eupmyeondong=%s",
            keyword,
            search_kw,
            sigun_filters,
            eupmyeondong_filters,
        )
        keyword_string = JString(search_kw)

        startnum = 0
        endnum = int(max_top_n) - 1
        query.setResult(startnum, endnum)
        query.setFrom(collection)
        query.setSearchKeyword(keyword_string)
        query.setSearch(True)
        query.setDebug(True)
        query.setPrintQuery(True)
        query.setLoggable(True)
        query.setFaultless(True)

        query.setValue("VS_THRESHOLD", str(threshold))
        query.setValue("VS_RESULT_SIZE", str(max_top_n))

        # SELECT 필드 (GSND_OUR_REGION_TEL 전용)
        num = MARINER_SELECT_FIELD_NUM
        select_field_names = list(_WELFARE_TEL_SELECT_FIELDS)

        select_set_array = [
            jpkg_query.SelectSet(JString(field_name), num, 0)
            for field_name in select_field_names
        ]
        field_indexes = {field_name: idx for idx, field_name in enumerate(select_field_names)}
        query.setSelect(select_set_array)

        # 정렬: WEIGHT 수치 내림차순 (JByte(97))
        order_set_array = [jpkg_query.OrderBySet(False, "WEIGHT", jpype.JByte(_ORDERBY_WEIGHT_DESC))]
        query.setOrderby(order_set_array)

        # WHERE: SIGUN 중심 필터. 키워드 기반 OR 검색식은 사용하지 않는다.
        sigun_expanded = _expand_sigun_scriptlet_values(sigun_scriptlet_values)
        where_set_array: List[Any] = []
        if len(sigun_expanded) == 1:
            where_set_array.append(
                jpkg_query.WhereSet("SIGUN", OP_HASALL, JString(sigun_expanded[0]), _SCRIPTLET_WEIGHT_NONE),
            )
        else:
            where_set_array.append(jpkg_query.WhereSet(OP_BRACE_OPEN))
            for idx, sv in enumerate(sigun_expanded):
                if idx > 0:
                    where_set_array.append(jpkg_query.WhereSet(OP_OR))
                where_set_array.append(
                    jpkg_query.WhereSet("SIGUN", OP_HASALL, JString(sv), _SCRIPTLET_WEIGHT_NONE),
                )
            where_set_array.append(jpkg_query.WhereSet(OP_BRACE_CLOSE))

        # 시 단위 입력에서 하위 구 데이터 누락 시, 주소 시명 매칭을 보조 OR로 사용.
        base_sigun = str(sigun_scriptlet_values[0]).strip() if sigun_scriptlet_values else ""
        parts = base_sigun.split()
        if len(parts) >= 2 and parts[0].endswith("도") and parts[1].endswith("시"):
            city_token = parts[1]
            where_set_array = [
                jpkg_query.WhereSet(OP_BRACE_OPEN),
                *where_set_array,
                jpkg_query.WhereSet(OP_OR),
                jpkg_query.WhereSet("ADDRESS", OP_HASALL, JString(city_token), _SCRIPTLET_WEIGHT_NONE),
                jpkg_query.WhereSet(OP_BRACE_CLOSE),
            ]

        # EUPMYEONDONG 스크립틀릿 (op 1, 부분형 "동읍") + 질의 내 `옥포 주민센터` → 옥포n동 AND
        eupmyeondong_values = eup_expanded_for_where
        if eupmyeondong_values:
            if len(eupmyeondong_values) == 1:
                where_set_array += [
                    jpkg_query.WhereSet(OP_AND),
                    jpkg_query.WhereSet(
                        "EUPMYEONDONG",
                        _SCRIPTLET_OP_PARTIAL,
                        JString(eupmyeondong_values[0]),
                        _SCRIPTLET_WEIGHT_NONE,
                    ),
                ]
            else:
                where_set_array.append(jpkg_query.WhereSet(OP_AND))
                where_set_array.append(jpkg_query.WhereSet(OP_BRACE_OPEN))
                for idx, ev in enumerate(eupmyeondong_values):
                    if idx > 0:
                        where_set_array.append(jpkg_query.WhereSet(OP_OR))
                    where_set_array.append(
                        jpkg_query.WhereSet(
                            "EUPMYEONDONG",
                            _SCRIPTLET_OP_PARTIAL,
                            JString(str(ev)),
                            _SCRIPTLET_WEIGHT_NONE,
                        ),
                    )
                where_set_array.append(jpkg_query.WhereSet(OP_BRACE_CLOSE))

        query.setWhere(where_set_array)

        queryset = jpkg_query.QuerySet(1)
        queryset.addQuery(query)
        ret = command.request(queryset)

        if ret < 0:
            err_detail = ""
            try:
                em = command.getErrorMessage()
                if em is not None:
                    err_detail = f" | ErrorMessage={em}"
            except Exception:
                pass
            logger.error(f"[Mariner/our_region_tel] 요청 오류: {ret}{err_detail}")
            raise RAGServiceError(f"Mariner API 반환 코드: {ret}{err_detail}")

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
                weight_val = float(str(raw_weight)) * _WEIGHT_SCALE
            except (ValueError, TypeError):
                weight_val = 0.0

            doc = {field_name: str(result.getResult(i, idx) or "") for field_name, idx in field_indexes.items()}
            doc["WEIGHT"] = str(weight_val)
            doc["CHUNK_ID"] = doc.get("ID", "")
            doc["_source"] = "our_region_tel"
            _cn = str(doc.get("CENTER") or "").strip()
            if _cn:
                doc["NAME"] = _cn

            # CHUNK_PATH: 핵심 필드 스니펫
            snippet_parts = []
            for field, label in _WELFARE_TEL_SNIPPET_FIELDS:
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
