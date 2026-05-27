"""단계별 일관성 진단 트레이스 수집기.

답변 비결정성이 *어느 단계에서* 발생하는지 보려고, 한 요청의 각 단계 산출을
모아 RESPONSE_TRACE 의 extras 에 실어 보낸다. 같은 질문을 N회 호출(새 conv_id)한 뒤
response_trace.jsonl 을 회차별로 비교하면, 어느 컬럼이 회차마다 흔들리는지 보인다.

캡처 단계 (사용자 요청 8단계):
- 쿼리 재구성      → reformed_query
- 의도 분류        → intent
- 쿼리 리라이팅    → expanded_queries
- 키워드 추출      → keywords
- 마리너 입력      → search_queries[].keywords  (실제 WhereSet 에 들어간 검색어)
- 검색식           → search_queries[].where     (WhereSet 트리 재구성 문자열)
- 검색된 문서      → (trace_sink 의 top_docs)
- 최종 응답        → (trace_sink 의 response_text)

스레드 모델:
- preprocess 단계는 메인 코루틴에서 record_preprocess() 로 기록.
- 검색식 단계는 Mariner 빌더가 loop.run_in_executor(스레드 풀)에서 실행되므로
  contextvar 가 전파되지 않는다. 따라서 threading.Lock 으로 보호되는 모듈 전역에
  누적한다. 진단 러너가 요청을 '순차'로 던지면(동시 in-flight 1건) 요청별 귀속이
  정확하다. RESPONSE_TRACE_ENABLED 진단 용도 한정 — 다중 동시요청 환경에서는
  검색식 귀속이 섞일 수 있다.

라이프사이클:
- reset()                         : unified_preprocess 진입 시 1회 (이전 요청 잔여 제거)
- record_preprocess(result)       : unified_preprocess 반환 직전
- record_search_query(label, arr) : 각 queryset 빌더의 query.setWhere(arr) 직전
- snapshot()                      : 트레이스 시점(response_generator)에서 호출 → dict

안전: 어떤 예외도 검색/응답 흐름을 깨면 안 되므로 모든 공개 함수는 try/except 로
감싸 no-op 으로 degrade 한다. 토글이 꺼져 있으면 즉시 반환한다.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_STATE: Dict[str, Any] = {}

# op 코드 → 사람이 읽는 이름 (src/app/core/constants.py 와 일치)
_OP_NAMES: Dict[int, str] = {
    1: "HASALL",
    2: "HASANY",
    3: "HASANYONE",
    5: "AND",
    6: "OR",
    7: "NOT",
    8: "WEIGHTAND",
    9: "(",
    10: ")",
    11: "HASALLONE",
    33: "INT_SUMMATION",
    96: "VECTOR",
}

# byte-or 부가 플래그 (메인 op 와 OR 결합되는 동의어/유의어 확장)
_FLAG_EQUIV = 0x10  # EQUIV_SYNONYM
_FLAG_QUASI = 0x20  # QUASI_SYNONYM


def _is_enabled() -> bool:
    try:
        from app.core.config import Config

        return bool(getattr(Config, "RESPONSE_TRACE_ENABLED", False))
    except Exception:
        return False


# ============================================================
# op / WhereSet 직렬화 헬퍼 (순수 함수 — 단위 테스트 대상)
# ============================================================


def _op_name(op: Any) -> str:
    """op 코드를 사람이 읽는 이름으로. 동의어 플래그(byte-or)는 분해해서 표기."""
    try:
        op_i = int(op)
    except Exception:
        return str(op)
    # 정확 일치 우선 (예: 33=INT_SUMMATION 은 1|0x20 과 비트가 겹치므로 먼저 잡아야 함)
    if op_i in _OP_NAMES:
        return _OP_NAMES[op_i]
    flags: List[str] = []
    base = op_i
    if base & _FLAG_QUASI:
        flags.append("QUASI_SYN")
        base &= ~_FLAG_QUASI
    if base & _FLAG_EQUIV:
        flags.append("EQUIV_SYN")
        base &= ~_FLAG_EQUIV
    name = _OP_NAMES.get(base, f"OP({op_i})")
    return "|".join([name, *flags]) if flags else name


def _jchars_to_str(value: Any) -> str:
    """JPype char[] / char[][] / None 을 파이썬 문자열로 관대하게 변환."""
    if value is None:
        return ""
    # char[] : 문자 단위로 순회되는 경우
    try:
        return "".join(str(c) for c in value)
    except TypeError:
        return str(value)


def _ws_field(ws: Any) -> str:
    try:
        return _jchars_to_str(ws.getField())
    except Exception:
        return ""


def _ws_op(ws: Any) -> Any:
    """WhereSet 의 연산 코드. Mariner JSP WhereSet 은 getOperation() (1.6.5).
    (getOption() 은 SelectSet 메서드라 WhereSet 엔 없음 — 호환 위해 폴백만 둠)
    """
    for meth in ("getOperation", "getOption"):
        try:
            fn = getattr(ws, meth, None)
            if fn is None:
                continue
            val = fn()
            if val is not None:
                return val
        except Exception:
            continue
    return None


def _ws_weight(ws: Any) -> Any:
    """검색 가중치 비율 getWeightRatio() (1.6.9). 미설정(-1)/연산자면 None."""
    try:
        wr = ws.getWeightRatio()
    except Exception:
        return None
    if not wr:
        return None
    try:
        vals = [round(float(x), 3) for x in wr if float(x) >= 0]
    except (TypeError, ValueError):
        return None
    if not vals:
        return None
    return vals[0] if len(vals) == 1 else vals


def _ws_keywords(ws: Any) -> str:
    """WhereSet 의 검색어(char[][]) 를 공백 join 한 문자열로."""
    try:
        kw = ws.getKeywords()
    except Exception:
        return ""
    if not kw:
        return ""
    parts: List[str] = []
    try:
        for row in kw:
            s = _jchars_to_str(row)
            if s:
                parts.append(s)
    except TypeError:
        s = _jchars_to_str(kw)
        if s:
            parts.append(s)
    return " ".join(parts)


def render_whereset(where_set_array: Any) -> str:
    """WhereSet 배열 → 사람이 읽는 검색식 문자열.

    필드 WhereSet 은 `FIELD[OP]="검색어"`, 연산자 WhereSet(OR/AND/괄호 등)은
    이름만 토큰으로 이어 붙인다. Mariner 가 해석하는 중위 표기를 그대로 보여준다.
    """
    tokens: List[str] = []
    for ws in where_set_array or []:
        field = _ws_field(ws)
        opname = _op_name(_ws_op(ws))
        if field:
            kw = _ws_keywords(ws)
            weight = _ws_weight(ws)
            wtxt = f"^{weight}" if weight is not None else ""
            tokens.append(f'{field}[{opname}]="{kw}"{wtxt}')
        else:
            tokens.append(opname)
    return " ".join(tokens)


# 검색 "쿼리"가 아니라 "필터/부스트 조건"으로 분류할 필드.
# (SIGUN 은 OR 그룹에선 벡터검색어지만, INT_SUMMATION 으로 쓰일 땐 지역 필터다.)
_FILTER_FIELDS = ("LIFE_CYCLE", "HOUSE_SITUATION")
_OP_INT_SUMMATION = 33


def _leg_from_label(label: str) -> str:
    """OKMS 듀얼 라벨에서 leg 종류 추정. #0=keyword, #1=vector, 그 외=single."""
    if label.endswith("#0"):
        return "keyword"
    if label.endswith("#1"):
        return "vector"
    return "single"


def _classify_clauses(where_set_array: Any):
    """필드 WhereSet 을 (메인 검색쿼리 term들, 필터 조건들) 로 분리.

    - 메인 검색쿼리: 사업명/서비스명/본문/SIGUN(벡터) 등 — leg 의 실제 검색어.
      OR 그룹 안에서 모든 필드가 같은 검색어를 공유하므로 보통 1종.
    - 필터: LIFE_CYCLE / HOUSE_SITUATION / SIGUN(INT_SUMMATION 지역 필터).
    """
    query_terms: List[str] = []
    filters: List[Dict[str, str]] = []
    for ws in where_set_array or []:
        field = _ws_field(ws)
        if not field:
            continue  # 연산자 WhereSet
        kw = _ws_keywords(ws)
        try:
            op_i = int(_ws_op(ws))
        except (TypeError, ValueError):
            op_i = None
        is_filter = field in _FILTER_FIELDS or (field == "SIGUN" and op_i == _OP_INT_SUMMATION)
        if is_filter:
            filters.append({"field": field, "value": kw})
        elif kw and kw not in query_terms:
            query_terms.append(kw)
    return query_terms, filters


# ============================================================
# 공개 API — 기록 / 스냅샷
# ============================================================


def reset() -> None:
    """요청 시작 시 1회. 이전 요청 잔여 제거."""
    if not _is_enabled():
        return
    try:
        with _LOCK:
            _STATE.clear()
            _STATE["search_queries"] = []
    except Exception as e:  # noqa: BLE001
        logger.debug("[stage_trace] reset 실패: %s", e)


def record_preprocess(result: Dict[str, Any]) -> None:
    """unified_preprocess 반환 dict 에서 단계 산출을 추출해 기록."""
    if not _is_enabled() or not isinstance(result, dict):
        return
    try:
        with _LOCK:
            _STATE["intent"] = result.get("intent")
            _STATE["intent_reason"] = result.get("intent_reason")
            _STATE["reformed_query"] = result.get("reformed_query")
            _STATE["expanded_queries"] = list(result.get("expanded_queries") or [])
            _STATE["keywords"] = list(result.get("keywords") or [])
            _STATE["policy_priority_tag"] = result.get("policy_priority_tag")
            _STATE["vector_query"] = result.get("vector_query")
            _STATE["search_target"] = result.get("search_target")
    except Exception as e:  # noqa: BLE001
        logger.debug("[stage_trace] record_preprocess 실패: %s", e)


def record_search_query(label: str, where_set_array: Any) -> None:
    """queryset 빌더의 setWhere 직전에 호출 — 실제 검색식과 검색어를 기록.

    스레드(executor)에서 호출될 수 있으므로 Lock 으로 보호. 검색/응답 흐름을
    절대 깨지 않도록 모든 예외를 흡수한다.
    """
    if not _is_enabled():
        return
    try:
        query_terms, filters = _classify_clauses(where_set_array)
        entry = {
            "label": str(label),
            "leg": _leg_from_label(str(label)),     # keyword | vector | single
            "query": " · ".join(query_terms),        # 이 leg 의 실제 검색쿼리
            "filters": filters,                       # [{field, value}, ...]
            "n_clauses": len(where_set_array or []),
            "where": render_whereset(where_set_array),  # 전체 WhereSet 트리 원문
        }
        with _LOCK:
            _STATE.setdefault("search_queries", []).append(entry)
    except Exception as e:  # noqa: BLE001
        logger.debug("[stage_trace] record_search_query 실패: %s", e)


_DOC_NAME_KEYS = ("BUSINESS_NAME", "SERVICE_NAME", "NAME", "name", "title")


def record_docs(stage: str, docs: Any) -> None:
    """리랭킹/필터 단계별 문서 목록을 기록 (이름 + WEIGHT + rrf_score).

    stage 예: 'rerank_before'(필터·RRF 직전 후보풀), 'rerank_after'(RRF 융합 결과).
    메인 코루틴에서 호출 — Lock 으로 보호하나 thread 진입은 없음.
    """
    if not _is_enabled():
        return
    try:
        items: List[Dict[str, Any]] = []
        for d in docs or []:
            if not isinstance(d, dict):
                continue
            name = next((str(d[k]) for k in _DOC_NAME_KEYS if d.get(k)), "")
            items.append({
                "name": name[:80],
                "weight": d.get("WEIGHT"),
                "rrf_score": d.get("rrf_score"),
            })
        with _LOCK:
            _STATE.setdefault("doc_stages", {})[str(stage)] = items
    except Exception as e:  # noqa: BLE001
        logger.debug("[stage_trace] record_docs 실패: %s", e)


def snapshot() -> Dict[str, Any]:
    """현재까지 누적된 단계 데이터를 dict 로 반환 (extras 병합용)."""
    if not _is_enabled():
        return {}
    try:
        with _LOCK:
            snap = dict(_STATE)
            snap["search_queries"] = list(_STATE.get("search_queries") or [])
        return snap
    except Exception:  # noqa: BLE001
        return {}
