"""
Mariner 쿼리셋 — GSND_WELFARE_CENTER_V1/V2 등 Config 지정 컬렉션 전용

IR5: SIGUN은 AND 스크립틀릿(op1), setSearchKeyword·WhereSet 병행, setFaultless.
색인(GSND_WELFARE_CENTER_V1 기준): TEXT_CHUNK_MI(Milvus)·TEXT_CHUNK_KO·SIGUN·FACILITY_NAME. 지번/ADDRESS 검색색인 없음 → WHERE는 MI·KO·FACILITY만. SELECT ADDRESS는 스니펫용 유지. 타임아웃 최소 120s.
"""

import logging
import re
import time
from typing import Any, Dict, List, Optional

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
    OP_VECTOR_SEARCH,
    MARINER_WEIGHT_HIGH,
    MARINER_WEIGHT_MED,
    MARINER_WEIGHT_LOW,
)
from app.core.exceptions import RAGServiceError
from app.mariner.queryset_welfare_tel import _expand_sigun_scriptlet_values
from app.mariner.sigun_utils import normalize_sigun
from app.mariner.jvm_manager import ensure_jvm_thread

logger = logging.getLogger(__name__)

# 트리플·확장 쿼리가 과도하게 길면 Mariner가 지연되어 -60004(타임아웃) 발생할 수 있음.
_MAX_WELFARE_KEYWORD_CHARS = 520
_MAX_WELFARE_KEYWORD_TOKEN_COUNT = 28
_MAX_SQUEEZED_VARIANT_CHARS = 64

# 클라 기본 타임아웃(60s)·벡터 병행 시 -100(소켓 타임아웃) 방지
_WELFARE_MARINER_TIMEOUT_MS_MIN = 120_000
_TAIL_TOKEN_DROP: frozenset[str] = frozenset({
    "연락처",
    "전화번호",
    "번호",
    "문의",
    "문의처",
    "안내",
})


def _dedupe_ordered_tokens(words: List[str]) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for w in words:
        if not w or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def _drop_glued_partial_after_good_prefix(words: List[str]) -> List[str]:
    """`창원도우누리`가 목록에 있을 때 `창원도우누리노인` 같은 찌꺼기 접미 토큰 제거."""

    keep: List[str] = []
    word_set = set(words)
    for t in words:
        if any(
            t != u
            and len(t) > len(u) >= 2
            and t.startswith(u)
            and u in word_set
            and len(t) <= len(u) + 16
            for u in words
        ):
            continue
        keep.append(t)
    return keep


def _drop_tokens_subsumed_by_longer(words: List[str]) -> List[str]:
    """`통합`, `재가` ⊂ `노인통합재가센터`처럼 짧은 분절 토큰 제거."""

    def subsumed(short: str, longtok: str) -> bool:
        if short == longtok or len(short) < 2:
            return False
        return short in longtok and len(longtok) >= max(7, len(short) + 2)

    return [t for t in words if not any(subsumed(t, u) for u in words if u != t)]


def _keywords_for_welfare_center(keyword: str) -> str:
    """
    파이프라인에서 들어오는 과장문(중복 어절)·붙여쓴 시설명을 IR5 친화 길이로 압축한다.
    띄어쓴 본문 + (짧을 때만) 공백 제거 변형을 공백으로 병합.
    """
    k = " ".join((keyword or "").split()).strip()
    if not k:
        return ""
    k = re.sub(r"도우\s+누리", "도우누리", k)
    toks = k.split()
    while toks and toks[-1] in _TAIL_TOKEN_DROP:
        toks.pop()
    toks = _dedupe_ordered_tokens(toks)
    toks = _drop_glued_partial_after_good_prefix(toks)
    toks = _drop_tokens_subsumed_by_longer(toks)
    clipped: List[str] = []
    for t in toks:
        next_join = (" ".join(clipped + [t])).strip()
        if len(next_join) > _MAX_WELFARE_KEYWORD_CHARS or len(clipped) >= _MAX_WELFARE_KEYWORD_TOKEN_COUNT:
            break
        clipped.append(t)

    base = " ".join(clipped).strip() or k[: _MAX_WELFARE_KEYWORD_CHARS]

    squeezed = "".join(base.split())
    extras: List[str] = []
    token_n = len(base.split())
    if (
        " " in base
        and squeezed != base
        and len(squeezed) <= _MAX_SQUEEZED_VARIANT_CHARS
        and token_n <= 6
        and len(base) + 1 + len(squeezed) <= _MAX_WELFARE_KEYWORD_CHARS + 40
    ):
        extras.append(squeezed)

    merged_parts = list(dict.fromkeys([base] + extras))
    out = " ".join(p for p in merged_parts if p).strip()
    if len((keyword or "").strip()) > len(out) + 80:
        logger.info(
            "[query_welfare_center] 키워드 압축: 원본 %d자 → %d자",
            len(keyword.strip()),
            len(out),
        )
    return out


# ============================================================
# WELFARE_CENTER 전용 Mariner 검색 함수
# ============================================================

def query_welfare_center_documents(
    keyword: str,
    sigun_filters: Optional[List[str]] = None,
    facility_type_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Config.RAG_WELFARE_CENTER_COLLECTION 전용 Mariner 검색

    예제 검색식 패턴 기반 WhereSet + IR5 정비:
    - WHERE: TEXT_CHUNK_KO(OP_HASANY) OR TEXT_CHUNK_MI(OP_VECTOR_SEARCH) OR FACILITY_NAME(HASANY·op1); AND SIGUN 스크립틀릿(op 1).
      창원 등은 queryset_welfare_tel과 동일한 시→구 OR 확장(JSON 겸용).
    - setSearchKeyword + setFaultless(True), 결과 개수(top_n)는 Config 기반.

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

    try:
        timeout = max(int(Config.MARINER_TIMEOUT), _WELFARE_MARINER_TIMEOUT_MS_MIN)
        top_n = max(1, int(Config.MARINER_MAX_RESULTS))
        threshold = float(Config.MARINER_THRESHOLD)
        vs_result_size = min(200, max(50, top_n * 4))

        ensure_jvm_thread()

        jpkg_cmd = jpype.JPackage("com.diquest.ir5.client.command")
        command = jpkg_cmd.CommandSearchRequest(Config.MARINER_IP, int(Config.MARINER_PORT))
        command.setProps(Config.MARINER_IP, int(Config.MARINER_PORT), timeout, MARINER_SETPROPS_EXTRA, MARINER_SETPROPS_EXTRA)

        jpkg_query = jpype.JPackage("com.diquest.ir5.common.msg.protocol.query")
        query = jpkg_query.Query("", "")
        merged_kw = _keywords_for_welfare_center(keyword)
        keyword_string = JString(merged_kw)
        logger.info(
            "[query_welfare_center] keyword=%r merged=%r sigun_filters=%s",
            keyword,
            merged_kw,
            sigun_filters,
        )

        startnum = 0
        endnum = int(top_n) - 1
        query.setResult(startnum, endnum)
        query.setFrom(collection)
        query.setSearchKeyword(keyword_string)
        query.setSearch(True)
        query.setDebug(False)
        query.setPrintQuery(False)
        query.setLoggable(False)
        query.setFaultless(True)

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

        # V1 색인: TEXT_CHUNK_MI(Milvus)·TEXT_CHUNK_KO·FACILITY_NAME(어절). 지번/ADDRESS 검색색인 없음.
        where_set_array = [
            jpkg_query.WhereSet(OP_BRACE_OPEN),
            jpkg_query.WhereSet("TEXT_CHUNK_KO", OP_HASANY, keyword_string, MARINER_WEIGHT_HIGH),
            jpkg_query.WhereSet(OP_OR),
            jpkg_query.WhereSet("TEXT_CHUNK_MI", OP_VECTOR_SEARCH, keyword_string, MARINER_WEIGHT_MED),
            jpkg_query.WhereSet(OP_OR),
            jpkg_query.WhereSet("FACILITY_NAME", OP_HASANY, keyword_string, MARINER_WEIGHT_HIGH),
            jpkg_query.WhereSet(OP_OR),
            jpkg_query.WhereSet("FACILITY_NAME", 1, keyword_string, MARINER_WEIGHT_MED),
            jpkg_query.WhereSet(OP_BRACE_CLOSE),
        ]

        sigun_scriptlet_values: List[str] = []
        skip_sigun = False
        for s in sigun_filters or []:
            t = str(s).strip()
            if not t:
                continue
            if t == "경상남도":
                skip_sigun = True
                break
            sigun_scriptlet_values.append(t)

        if sigun_scriptlet_values and not skip_sigun:
            sigun_expanded = _expand_sigun_scriptlet_values(sigun_scriptlet_values)
            where_set_array.append(jpkg_query.WhereSet(OP_AND))
            if len(sigun_expanded) == 1:
                where_set_array.append(
                    jpkg_query.WhereSet("SIGUN", 1, JString(sigun_expanded[0]), 0),
                )
            else:
                where_set_array.append(jpkg_query.WhereSet(OP_BRACE_OPEN))
                for idx, sv in enumerate(sigun_expanded):
                    if idx > 0:
                        where_set_array.append(jpkg_query.WhereSet(OP_OR))
                    where_set_array.append(jpkg_query.WhereSet("SIGUN", 1, JString(sv), 0))
                where_set_array.append(jpkg_query.WhereSet(OP_BRACE_CLOSE))

        query.setWhere(where_set_array)

        order_set_array = [jpkg_query.OrderBySet(False, "WEIGHT", jpype.JByte(97))]
        query.setOrderby(order_set_array)

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
            logger.error("[Mariner/welfare] 요청 오류: %s%s", ret, err_detail)
            raise RAGServiceError(f"Mariner API 반환 코드: {ret}{err_detail}")

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
            doc["NAME"] = str(doc.get("FACILITY_NAME", "") or "").strip()
            doc["_source"] = "welfare_center"

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
                doc_facility = str(doc.get("FACILITY_NAME", "") or "").strip()
                sigun_matched = False
                for ts in target_siguns:
                    if ts == "경상남도":
                        sigun_matched = True
                        break
                    short = ts.split(" ", 1)[1] if " " in ts else ts
                    city_core = short.replace("시", "").replace("군", "").strip()
                    if doc_sigun == ts or doc_sigun == short or short in doc_address:
                        sigun_matched = True
                        break
                    if (
                        len(city_core) >= 2
                        and city_core in doc_facility
                        and ts.startswith("경상남도 ")
                    ):
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

        t2 = time.monotonic()
        logger.info(f"[Mariner/welfare] 검색 시간: {t2 - t1:.3f}초, 키워드: {keyword[:50]}, 결과: {len(doc_list)}개")
        for i, doc in enumerate(doc_list, 1):
            logger.debug(f"[Mariner/welfare] #{i} ID={doc.get('CHUNK_ID', '?')}, NAME={doc.get('NAME', '?')}, WEIGHT={doc.get('WEIGHT', '?')}")

        return doc_list

    except RAGServiceError:
        raise
    except Exception as e:
        logger.error(f"[Mariner/welfare] 예상치 못한 오류: {e}", exc_info=True)
        raise RAGServiceError(f"복지시설 검색 중 오류: {str(e)}") from e
