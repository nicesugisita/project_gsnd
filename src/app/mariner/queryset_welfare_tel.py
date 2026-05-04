"""
Mariner 쿼리셋 — GSND_OUR_REGION_TEL 전용

색인 추출(컬렉션 설정 기준): SIGUN·EUPMYEONDONG·TEL=필드, CENTER=한국어 명사, ADDRESS=어절.
WHERE는 (CENTER·ADDRESS의 OP_HASANY) OR (동일 필드 스크립틀릿 op 1 부분일치) OR (EUPMYEONDONG op 1)
로 형태소 미적중 시에도 기관명·주소·읍면동 문자열을 잡는다.
사용자 표기 '행복복지' ↔ DB '행정복지', 「복지센터」단독 표기 ↔ 행정복지센터·행복복지센터 병기. 생략 ○동은 기관·연락처 맥락일 때 ○1동…○N동 보강.
`옥포 주민센터`처럼 동 접미 없이 쓴 읍·면·동 구역명도 동일 맥락에서 ○1동…으로 보강하고 EUPMYEONDONG AND로 시군 전체 남발을 줄인다.
단, `양산 동사무소`처럼 단어가 경남 시·군 약칭(양산→양산시)이면 번동 확장 금지(오탐).
엣지는 Config.RAG_WELFARE_TEL_DONG_ABBREV_JSON / RAG_WELFARE_TEL_DONG_EXPAND_EXCLUDE_BASES_JSON.
EUPMYEONDONG 필터로 들어온 `○동`(번호 없음)도 동일 규칙으로 1…N동 OR 확장하고,
짧은 키워드만 전달될 때를 대비해 Mariner 검색어에 `○1동`… 토큰을 병합한다.
SIGUN 시단위→구 OR 확장은 기본이 「경상남도 창원시」뿐이며, Config.RAG_WELFARE_TEL_SIGUN_OR_SUFFIXES_JSON 으로 다른 시·군 추가.
ID·WEIGHT는 SELECT/정렬 전용.
"""

import json
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
    OP_HASANY,
    MARINER_WEIGHT_HIGH,
    MARINER_WEIGHT_MED,
    MARINER_WEIGHT_LOW,
)
from app.core.exceptions import RAGServiceError
from app.mariner.jvm_manager import ensure_jvm_thread
from app.mariner.sigun_utils import is_gyeongnam_sigun_short_token

logger = logging.getLogger(__name__)

# Mariner setResult/VS_RESULT_SIZE 상한: 엔진·컬렉션 실물 개수보다 크면 전부 반환
_OUR_REGION_TEL_UNBOUND_FETCH = 500_000

# 기본: 세션은 「…시」만 주고 색인이 「…시 의창구」까지 있을 때 구 접미 OR 보강.
# 복지시설 등 SIGUN이 시 단위만인 문서도 있으므로 _expand_sigun 에서 「…시」 원문도 OR에 넣음.
_DEFAULT_SIGUN_OR_SUFFIXES: Dict[str, Tuple[str, ...]] = {
    "경상남도 창원시": (
        "의창구",
        "성산구",
        "마산합포구",
        "마산회원구",
        "진해구",
    ),
}

# 수동 동명 매핑만(엣지). 일반 ○동→○n동은 _digit_split_dong_variants 규칙으로 처리.
_DEFAULT_DONG_ABBREV_EXPANSIONS: Tuple[Tuple[str, Tuple[str, ...]], ...] = ()

# 「○동」이 색인의 「○1동」과 겹치지 않을 때: 기관·연락처 맥락이면 1…N동 변형 문장을 검색어에 병기
_DONG_OFFICE_CONTEXT_MARKERS: Tuple[str, ...] = (
    "행정복지",
    "행복복지",
    "동사무소",
    "주민센터",
    "복지센터",
    "행정복지센터",
    "연락처",
    "전화",
    "민원",
)
_DONG_DIGIT_EXPAND_MAX: int = 9
_DONG_BASE_RE = re.compile(r"([가-힣]{2,10})동")
_DONG_OFFICE_MARKERS_ALT = "|".join(
    re.escape(m) for m in sorted(_DONG_OFFICE_CONTEXT_MARKERS, key=len, reverse=True)
)
_BARE_PLACE_BEFORE_OFFICE_RE = re.compile(rf"([가-힣]{{2,10}})\s+({_DONG_OFFICE_MARKERS_ALT})")


def _strip_welfare_tel_query_suffixes(k: str) -> str:
    k = (k or "").strip()
    for suf in (" 연락처 문의", " 연락처", " 전화번호", " 번호", " 문의"):
        if k.endswith(suf):
            return k[: -len(suf)].strip()
    return k


def _has_standalone_bokji_center_phrase(v: str) -> bool:
    """
    `양산 복지센터`처럼 단독 어절인 경우만 행정복지센터 병기 대상.
    `사회복지센터`는 한 덩어리라 제외(앞이 비어있지 않은 한글 접속).
    """
    if "행정복지센터" in v or "행복복지센터" in v:
        return False
    return bool(re.search(r"(?:^|\s)복지센터(?:\s|$)", v))


def _merged_sigun_or_suffixes() -> Dict[str, Tuple[str, ...]]:
    out: Dict[str, Tuple[str, ...]] = {k: tuple(v) for k, v in _DEFAULT_SIGUN_OR_SUFFIXES.items()}
    raw = (Config.RAG_WELFARE_TEL_SIGUN_OR_SUFFIXES_JSON or "").strip()
    if not raw:
        return out
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return out
        for key, val in data.items():
            ks = str(key).strip()
            if not ks or not isinstance(val, list):
                continue
            suf = tuple(str(x).strip() for x in val if str(x).strip())
            if suf:
                out[ks] = suf
    except json.JSONDecodeError:
        logger.warning("RAG_WELFARE_TEL_SIGUN_OR_SUFFIXES_JSON 파싱 실패 — 코드 기본 SIGUN 확장만 사용")
    return out


def _merged_dong_abbrev_expansions() -> Tuple[Tuple[str, Tuple[str, ...]], ...]:
    merged: Dict[str, List[str]] = {a: list(forms) for a, forms in _DEFAULT_DONG_ABBREV_EXPANSIONS}
    raw = (Config.RAG_WELFARE_TEL_DONG_ABBREV_JSON or "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                for key, val in data.items():
                    ks = str(key).strip()
                    if not ks or not isinstance(val, list):
                        continue
                    merged[ks] = [str(x).strip() for x in val if str(x).strip()]
        except json.JSONDecodeError:
            logger.warning("RAG_WELFARE_TEL_DONG_ABBREV_JSON 파싱 실패 — 코드 기본 동명 보강만 사용")
    return tuple((abbrev, tuple(forms)) for abbrev, forms in sorted(merged.items()))


def _merged_dong_expand_exclude_bases() -> frozenset[str]:
    """자동 ○동→○n동 보강에서 제외할 베이스(예: 중앙). Config JSON 배열과 병합."""
    out: set[str] = set()
    raw = (Config.RAG_WELFARE_TEL_DONG_EXPAND_EXCLUDE_BASES_JSON or "").strip()
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                for x in data:
                    t = str(x).strip()
                    if t:
                        out.add(t)
        except json.JSONDecodeError:
            logger.warning(
                "RAG_WELFARE_TEL_DONG_EXPAND_EXCLUDE_BASES_JSON 파싱 실패 — 제외 목록 없이 자동 보강",
            )
    return frozenset(out)


def _digit_split_dong_context(k: str) -> bool:
    return any(m in k for m in _DONG_OFFICE_CONTEXT_MARKERS)


def _digit_split_dong_variants(k: str) -> List[str]:
    """
    `회원동`처럼 숫자 없이 끝나는 읍·면·동 이름을 `회원1동`…`회원N동` 문장으로 병기(시군구 무관).
    질의에 이미 `회원1동` 형태가 있으면 해당 베이스는 스킵.
    """
    if not k or not _digit_split_dong_context(k):
        return []
    excludes = _merged_dong_expand_exclude_bases()
    out: List[str] = []
    for m in _DONG_BASE_RE.finditer(k):
        base = m.group(1)
        if not base or base in excludes:
            continue
        if re.search(re.escape(base) + r"[0-9]+동", k):
            continue
        start, end = m.span()
        for n in range(1, _DONG_DIGIT_EXPAND_MAX + 1):
            token = f"{base}{n}동"
            if token in k:
                continue
            repl = k[:start] + token + k[end:]
            if repl != k and repl not in out:
                out.append(repl)
    return out


def _bare_place_before_office_variants(k: str) -> List[str]:
    """
    `거제시 옥포 주민센터`처럼 색인은 `옥포1동`인데 사용자가 `동`을 생략한 경우.
    색인이 단일 `덕계동`처럼 번호 없는 동명이면 `{base}동` 변형도 병기한다.
    읍·면·동으로 끝나는 토큰은 제외(이미 _digit_split_dong_variants에서 처리).
    """
    if not k or not _digit_split_dong_context(k):
        return []
    excludes = _merged_dong_expand_exclude_bases()
    out: List[str] = []
    for m in _BARE_PLACE_BEFORE_OFFICE_RE.finditer(k):
        base = m.group(1)
        if not base or base in excludes:
            continue
        if is_gyeongnam_sigun_short_token(base):
            continue
        if re.search(r"(읍|면|동)$", base):
            continue
        if re.search(r"(시|군|구|도)$", base):
            continue
        if re.search(re.escape(base) + r"[0-9]+동", k):
            continue
        sb, eb = m.span(1)
        plain_dong = f"{base}동"
        if plain_dong not in k:
            repl_plain = k[:sb] + plain_dong + k[eb:]
            if repl_plain != k and repl_plain not in out:
                out.append(repl_plain)
        for n in range(1, _DONG_DIGIT_EXPAND_MAX + 1):
            token = f"{base}{n}동"
            if token in k:
                continue
            repl = k[:sb] + token + k[eb:]
            if repl != k and repl not in out:
                out.append(repl)
    return out


def _eupmyeondong_tokens_from_bare_place_keyword(keyword: str) -> List[str]:
    """질의 내 `옥포 주민···`·`덕계 행정···` 등 → EUPMYEONDONG OR용 `옥포1동`… + 단일 `덕계동`."""
    k = _strip_welfare_tel_query_suffixes(keyword)
    if not k or not _digit_split_dong_context(k):
        return []
    excludes = _merged_dong_expand_exclude_bases()
    seen: set[str] = set()
    out: List[str] = []
    for m in _BARE_PLACE_BEFORE_OFFICE_RE.finditer(k):
        base = m.group(1)
        if not base or base in excludes:
            continue
        if is_gyeongnam_sigun_short_token(base):
            continue
        if re.search(r"(읍|면|동)$", base):
            continue
        if re.search(r"(시|군|구|도)$", base):
            continue
        if re.search(re.escape(base) + r"[0-9]+동", k):
            continue
        plain_tok = f"{base}동"
        if plain_tok not in seen:
            seen.add(plain_tok)
            out.append(plain_tok)
        for n in range(1, _DONG_DIGIT_EXPAND_MAX + 1):
            tok = f"{base}{n}동"
            if tok not in seen:
                seen.add(tok)
                out.append(tok)
    return out


def _merge_extra_eup_tokens_into_keyword(search_kw: str, tokens: List[str]) -> str:
    k = (search_kw or "").strip()
    if not k or not tokens:
        return k
    words = set(k.split())
    to_add = [t for t in tokens if t not in words]
    if not to_add:
        return k
    return f"{k} {' '.join(to_add)}"


def _expand_sigun_scriptlet_values(sigun_values: List[str]) -> List[str]:
    """시 단위만 주어지고 맵에 있으면 구 접미 SIGUN OR로 확장. 그 외는 공백 결합(gsnd) 1문열 유지.

    확장만 하고 시 문자열 자체를 빼면, SIGUN 색인이 「경상남도 창원시」만인 문서(구 미기재)는
    어느 OR에도 매칭되지 않아 raw 0건이 될 수 있으므로 **원문 v를 항상 OR 선두에 포함**한다.
    """
    if not sigun_values:
        return []
    if len(sigun_values) != 1:
        return [" ".join(s.strip() for s in sigun_values if s and str(s).strip())]
    v = str(sigun_values[0]).strip()
    suffix_map = _merged_sigun_or_suffixes()
    if v in suffix_map:
        suffixed = [f"{v} {suf}" for suf in suffix_map[v]]
        return [v] + suffixed
    return [v]


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
    if base in _merged_dong_expand_exclude_bases():
        return [ev]
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


def _merge_eup_ambiguous_numbered_dongs_into_keyword(
    search_kw: str,
    eup_raw_filters: List[str],
) -> str:
    """
    트리플 추출 등으로 키워드에 '동사무소·연락처'가 빠지면 ○n동 보강이 스킵될 수 있다.
    읍면동 필터가 `회원동`처럼 번 없는 동으로 들어온 경우, OR절(EUPMYEONDONG op1)이
    짧은 키워드만으로는 매칭되지 않을 수 있어 번호 동 토큰을 검색어에 병합한다.
    """
    k = (search_kw or "").strip()
    if not k or not eup_raw_filters:
        return k
    words = set(k.split())
    to_add: List[str] = []
    for raw in eup_raw_filters:
        ev = str(raw).strip()
        if not ev:
            continue
        toks = _eupmyeondong_filter_or_tokens(ev)
        if len(toks) <= 1:
            continue
        for t in toks[1:]:
            if t in words:
                continue
            words.add(t)
            to_add.append(t)
    if not to_add:
        return k
    return f"{k} {' '.join(to_add)}"


def _prepare_welfare_tel_mariner_keyword(
    keyword: str,
    eupmyeondong_filters: Optional[List[str]],
) -> str:
    eup_raw = [str(e).strip() for e in (eupmyeondong_filters or []) if e and str(e).strip()]
    from_kw = _eupmyeondong_tokens_from_bare_place_keyword(keyword)
    base = _augment_welfare_tel_search_keyword(keyword)
    merged = _merge_eup_ambiguous_numbered_dongs_into_keyword(base, eup_raw)
    return _merge_extra_eup_tokens_into_keyword(merged, from_kw)


def _augment_welfare_tel_search_keyword(keyword: str) -> str:
    """행복↔행정 복지센터 표기, 생략 ○동→○n동(맥락 시), 수동 동명 JSON 보강."""
    k = _strip_welfare_tel_query_suffixes(keyword)
    if not k:
        return k
    variants: List[str] = [k]
    variants.extend(_digit_split_dong_variants(k))
    variants.extend(_bare_place_before_office_variants(k))

    v_after_abbrev: List[str] = []
    for v in variants:
        v_after_abbrev.append(v)
        for abbrev, full_forms in _merged_dong_abbrev_expansions():
            if abbrev not in v:
                continue
            if any(f in v for f in full_forms):
                continue
            for f in full_forms:
                v_after_abbrev.append(v.replace(abbrev, f, 1))

    expanded: List[str] = []
    for v in v_after_abbrev:
        expanded.append(v)
        if "행복복지" in v and "행정복지" not in v:
            expanded.append(v.replace("행복복지", "행정복지"))
        # 색인 CENTER는 「행정복지센터」「행복복지센터」인데 질의만 「복지센터」인 경우
        if _has_standalone_bokji_center_phrase(v):
            expanded.append(v.replace("복지센터", "행정복지센터", 1))
            expanded.append(v.replace("복지센터", "행복복지센터", 1))

    seen: set[str] = set()
    out: List[str] = []
    for p in expanded:
        s = p.strip()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return " ".join(out)


def query_welfare_tel_documents(
    keyword: str,
    sigun_filters: Optional[List[str]] = None,
    eupmyeondong_filters: Optional[List[str]] = None,
    max_results: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    GSND_OUR_REGION_TEL 컬렉션 전용 Mariner 검색

    예제 검색식(MarinerQuerySetExampleCode_OUR_REGION_TEL.py) 기반:
    - WHERE: (CENTER·ADDRESS OP_HASANY) OR (CENTER·ADDRESS·EUPMYEONDONG 스크립틀릿 op 1) 후 AND로 SIGUN 등.
      질의에 '행복복지'만 있으면 '행정복지' 병기. 기관·연락처 맥락이면 `○동`→`○1동`…`○9동` 자동 병기 및 수동 JSON(_augment_welfare_tel_search_keyword).
    - SIGUN은 키워드 OR절에 넣지 않음(공통 시군명 과매칭 방지). sigun_filters가 있을 때만 AND WhereSet 스크립틀릿(op 1);
      복수 시군은 공백 결합 1절(gsnd). SIGUN 필터가 _merged_sigun_or_suffixes 키와 일치하면 구 OR 확장(기본은 창원시).
      필터 값이 정확히 「경상남도」만 있으면 SIGUN 절 생략.
      원천이 시군구·읍면동 컬럼 분리면 SIGUN은 보통 「경상남도 OO시」 수준이고 구·동은 EUPMYEONDONG에 있음;
      구·동만 한정할 때는 eupmyeondong_filters를 사용한다.
      `회원동`처럼 번 없는 동명 필터는 색인 `회원1동` 등과 맞추기 위해 내부에서 1…N동 OR로 확장한다.
      동시에 Mariner 검색어에 `회원1동`… 토큰을 병합해, 키워드만 짧아졌을 때도 OR 절이 매칭되게 한다.
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
        eup_raw_for_kw = [str(e).strip() for e in (eupmyeondong_filters or []) if e and str(e).strip()]
        eup_from_kw = _eupmyeondong_tokens_from_bare_place_keyword(keyword)
        eup_expanded_for_where = _expand_ambiguous_eupmyeondong_filters(eup_raw_for_kw)
        if eup_from_kw:
            _seen_e = set(eup_expanded_for_where)
            for _t in eup_from_kw:
                if _t not in _seen_e:
                    _seen_e.add(_t)
                    eup_expanded_for_where.append(_t)
        search_kw = _prepare_welfare_tel_mariner_keyword(keyword, eupmyeondong_filters)
        logger.info(
            "[query_our_region_tel] keyword=%r search_keyword=%r sigun=%s eupmyeondong=%s "
            "eup_from_keyword=%s",
            keyword,
            search_kw,
            sigun_filters,
            eupmyeondong_filters,
            eup_from_kw[:12] if eup_from_kw else [],
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

        # WHERE: IR5 WhereSet — OP_HASANY(명사·어절) + op 1 스크립틀릿(부분일치) OR 결합; SIGUN은 아래 AND로만 추가
        _where_or_terms: Tuple[Tuple[str, int, float], ...] = (
            ("CENTER", OP_HASANY, MARINER_WEIGHT_HIGH),
            ("ADDRESS", OP_HASANY, MARINER_WEIGHT_HIGH),
            ("CENTER", 1, MARINER_WEIGHT_MED),
            ("ADDRESS", 1, MARINER_WEIGHT_MED),
            ("EUPMYEONDONG", 1, MARINER_WEIGHT_LOW),
        )
        where_set_array: List[Any] = [jpkg_query.WhereSet(OP_BRACE_OPEN)]
        for i, (field, op, weight) in enumerate(_where_or_terms):
            if i:
                where_set_array.append(jpkg_query.WhereSet(OP_OR))
            where_set_array.append(jpkg_query.WhereSet(field, op, keyword_string, weight))
        where_set_array.append(jpkg_query.WhereSet(OP_BRACE_CLOSE))

        # SIGUN 스크립틀릿 (op 1 부분형): 세션·전처리 시군구
        # 시군구 컬럼만 SIGUN에 들어가면 「경상남도 창원시」로 창원 전체; 진해구 등은 EUPMYEONDONG 쪽 필터 검토.
        # SIGUN: FilterSet은 setFilter 전용이므로 WhereSet 스크립틀릿(op 1)만 사용 (queryset_gsnd 동일 패턴).
        # 「경상남도」만 오면 광역 전체로 간주해 SIGUN 절 제외(과도 한정 방지).
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

        # EUPMYEONDONG 스크립틀릿 (op 1, 부분형 "동읍") + 질의 내 `옥포 주민센터` → 옥포n동 AND
        eupmyeondong_values = eup_expanded_for_where
        if eupmyeondong_values:
            if len(eupmyeondong_values) == 1:
                where_set_array += [
                    jpkg_query.WhereSet(OP_AND),
                    jpkg_query.WhereSet("EUPMYEONDONG", 1, JString(eupmyeondong_values[0]), 0),
                ]
            else:
                where_set_array.append(jpkg_query.WhereSet(OP_AND))
                where_set_array.append(jpkg_query.WhereSet(OP_BRACE_OPEN))
                for idx, ev in enumerate(eupmyeondong_values):
                    if idx > 0:
                        where_set_array.append(jpkg_query.WhereSet(OP_OR))
                    where_set_array.append(
                        jpkg_query.WhereSet("EUPMYEONDONG", 1, JString(str(ev)), 0),
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
                weight_val = float(str(raw_weight)) * 0.0001
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
