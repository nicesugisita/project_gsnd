"""단계별 일관성 진단 — response_trace.jsonl → 비교 xlsx.

같은 질문을 N회 호출한 트레이스(response_trace.jsonl)를 읽어:
- "stages" 시트: 한 행 = 한 호출(회차). 8단계 컬럼을 펼침.
- "diff" 시트  : 질문별로 각 단계가 회차 간 몇 종류 값을 갖는지(>1 이면 흔들림) 표시.
                 흔들리는 셀은 색으로 강조 → "어느 단계에서 일관성이 깨지나" 한눈.

사용:
    python tests/export_stage_consistency.py [jsonl_경로] [출력_xlsx]
인자 생략 시 Config.RESPONSE_TRACE_DIR/response_trace.jsonl 을 읽고
같은 폴더에 stage_consistency_<ts>.xlsx 로 저장한다.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

_CELL_MAX = 30_000
_FLAP_FILL = PatternFill(start_color="FFF2A2", end_color="FFF2A2", fill_type="solid")  # 노랑
_HDR_FONT = Font(bold=True)

# (컬럼 제목, 추출 함수) — 8단계 + 메타
_STAGE_COLUMNS = [
    ("ts", lambda r, e: r.get("ts", "")),
    ("round", lambda r, e: r.get("_round", "")),
    ("의도분류(intent)", lambda r, e: e.get("intent") or r.get("intent") or ""),
    ("쿼리재구성(reformed)", lambda r, e: e.get("reformed_query") or ""),
    ("쿼리리라이팅(expanded)", lambda r, e: " ‖ ".join(e.get("expanded_queries") or [])),
    ("키워드추출(keywords)", lambda r, e: " ‖ ".join(e.get("keywords") or [])),
    ("정책태그(policy_tag)", lambda r, e: str(e.get("policy_priority_tag"))),
    ("벡터쿼리(vector_query)", lambda r, e: e.get("vector_query") or ""),
    ("마리너 검색쿼리(leg별)", lambda r, e: _fmt_search_query(e.get("search_queries"))),
    ("마리너 필터", lambda r, e: _fmt_search_filters(e.get("search_queries"))),
    ("검색식(WhereSet)", lambda r, e: _fmt_search_where(e.get("search_queries"))),
    ("리랭킹전(후보풀)", lambda r, e: _fmt_doc_stage(e.get("doc_stages"), "rerank_before")),
    ("리랭킹후", lambda r, e: _fmt_doc_stage(e.get("doc_stages"), "rerank_after")),
    ("검색문서(docs)", lambda r, e: _fmt_docs(r.get("top_docs"))),
    ("문서수", lambda r, e: len(r.get("top_docs") or [])),
    ("응답길이", lambda r, e: len(r.get("response_text") or "")),
    ("최종응답(response)", lambda r, e: r.get("response_text") or ""),
]

# diff 시트에서 회차 간 일관성을 따질 단계 (값이 동일해야 정상)
_DIFF_STAGES = [
    "의도분류(intent)",
    "쿼리재구성(reformed)",
    "쿼리리라이팅(expanded)",
    "키워드추출(keywords)",
    "정책태그(policy_tag)",
    "벡터쿼리(vector_query)",
    "마리너 검색쿼리(leg별)",
    "마리너 필터",
    "검색식(WhereSet)",
    "리랭킹전(후보풀)",
    "리랭킹후",
    "검색문서(docs)",
    "최종응답(response)",
]

_DOC_NAME_KEYS = ("BUSINESS_NAME", "SERVICE_NAME", "NAME", "name", "title")


def _fmt_docs(top_docs: Any) -> str:
    if not top_docs:
        return ""
    names: List[str] = []
    for d in top_docs:
        if not isinstance(d, dict):
            names.append(str(d)[:60])
            continue
        nm = next((str(d[k]) for k in _DOC_NAME_KEYS if d.get(k)), None)
        names.append((nm or json.dumps(d, ensure_ascii=False))[:60])
    return " ‖ ".join(names)


_LEG_KO = {"keyword": "키워드", "vector": "벡터", "single": "단일"}


def _round_or_none(v: Any, ndigits: int = 3):
    """WEIGHT/rrf 부동소수점 끝자리 노이즈 제거용 반올림 (None 보존)."""
    try:
        return round(float(v), ndigits)
    except (TypeError, ValueError):
        return None


def _fmt_doc_stage(doc_stages: Any, stage: str) -> str:
    """리랭킹 전/후 문서 목록을 'name (W=weight, rrf=score)' 줄들로.

    WEIGHT/rrf 는 3자리 반올림 — Mariner 점수의 FP 끝자리 노이즈로 회차 비교가
    거짓 '흔들림'이 되는 것을 막는다(문서 셋·순서 비교가 목적).
    """
    if not isinstance(doc_stages, dict):
        return ""
    items = doc_stages.get(stage) or []
    lines = []
    for i, d in enumerate(items, 1):
        if not isinstance(d, dict):
            continue
        w = _round_or_none(d.get("weight"))
        rrf = _round_or_none(d.get("rrf_score"))
        meta = []
        if w is not None:
            meta.append(f"W={w}")
        if rrf is not None:
            meta.append(f"rrf={rrf}")
        mtxt = f" ({', '.join(meta)})" if meta else ""
        lines.append(f"{i}. {d.get('name', '')}{mtxt}")
    return "\n".join(lines)


def _sorted_sq(search_queries: Any) -> List[Dict[str, Any]]:
    """검색식 entry 를 label·query 기준 정렬 — executor 스레드 기록 순서 artifact 제거."""
    items = [q for q in (search_queries or []) if isinstance(q, dict)]
    return sorted(items, key=lambda q: (str(q.get("label", "")), str(q.get("query", ""))))


def _fmt_search_query(search_queries: Any) -> str:
    """leg(키워드/벡터)별 실제 검색쿼리. 필터는 제외하고 메인 검색어만."""
    if not search_queries:
        return ""
    parts = []
    for sq in _sorted_sq(search_queries):
        leg = _LEG_KO.get(sq.get("leg", ""), sq.get("leg", ""))
        tag = f"{sq.get('label', '?')}·{leg}" if leg else sq.get("label", "?")
        parts.append(f"[{tag}] {sq.get('query', '')}")
    return "\n".join(parts)


def _fmt_search_filters(search_queries: Any) -> str:
    """검색에 적용된 필터/부스트 조건 (시군·생애주기·가구상황)."""
    if not search_queries:
        return ""
    parts = []
    for sq in _sorted_sq(search_queries):
        fs = sq.get("filters") or []
        ftxt = ", ".join(f"{x.get('field')}={x.get('value')}" for x in fs if isinstance(x, dict))
        parts.append(f"[{sq.get('label', '?')}] {ftxt}")
    return "\n".join(parts)


def _fmt_search_where(search_queries: Any) -> str:
    if not search_queries:
        return ""
    parts = []
    for sq in _sorted_sq(search_queries):
        parts.append(f"[{sq.get('label', '?')}] {sq.get('where', '')}")
    return "\n".join(parts)


def _truncate(v: Any) -> Any:
    if isinstance(v, str) and len(v) > _CELL_MAX:
        return v[:_CELL_MAX] + f"...[+{len(v) - _CELL_MAX}자]"
    return v


def load_records(jsonl_path: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def assign_rounds(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """질문별로 ts 정렬 후 회차(round) 부여."""
    by_q: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_q[(r.get("user_question") or "").strip()].append(r)
    out: List[Dict[str, Any]] = []
    for q, recs in by_q.items():
        recs.sort(key=lambda r: r.get("ts", ""))
        for i, r in enumerate(recs, 1):
            r["_round"] = i
        out.extend(recs)
    return out


def build_workbook(records: List[Dict[str, Any]]) -> Workbook:
    wb = Workbook()

    # ---- 시트 1: stages (질문별 그룹, 회차별 행) ----
    ws = wb.active
    ws.title = "stages"
    headers = ["질문"] + [c[0] for c in _STAGE_COLUMNS]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = _HDR_FONT

    by_q: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_q[(r.get("user_question") or "").strip()].append(r)

    for q in sorted(by_q):
        recs = sorted(by_q[q], key=lambda r: r.get("ts", ""))
        for r in recs:
            e = r.get("extras") or {}
            row = [q] + [_truncate(fn(r, e)) for _, fn in _STAGE_COLUMNS]
            ws.append(row)

    # ---- 시트 2: diff (질문별 단계 일관성) ----
    wd = wb.create_sheet("diff")
    diff_headers = ["질문", "회차수"] + _DIFF_STAGES
    wd.append(diff_headers)
    for cell in wd[1]:
        cell.font = _HDR_FONT

    # stages 컬럼명 → 추출함수 매핑
    col_fn = {name: fn for name, fn in _STAGE_COLUMNS}

    for q in sorted(by_q):
        recs = sorted(by_q[q], key=lambda r: r.get("ts", ""))
        n_rounds = len(recs)
        row = [q, n_rounds]
        flap_flags: List[bool] = []
        for stage in _DIFF_STAGES:
            fn = col_fn[stage]
            values = {str(fn(r, r.get("extras") or {})) for r in recs}
            distinct = len(values)
            flap = distinct > 1
            flap_flags.append(flap)
            row.append(f"{distinct}종" + (" ⚠흔들림" if flap else " ✓고정"))
        wd.append(row)
        # 흔들리는 셀 강조 (질문, 회차수 컬럼 다음부터)
        excel_row = wd.max_row
        for j, flap in enumerate(flap_flags):
            if flap:
                wd.cell(row=excel_row, column=3 + j).fill = _FLAP_FILL

    # ---- 시트 3: 변동상세 (흔들린 단계의 회차별 값을 나란히) ----
    wf = wb.create_sheet("변동상세")
    max_rounds = max((len(v) for v in by_q.values()), default=1)
    detail_headers = ["질문", "단계", "값종류수"] + [f"R{i}" for i in range(1, max_rounds + 1)]
    wf.append(detail_headers)
    for cell in wf[1]:
        cell.font = _HDR_FONT

    for q in sorted(by_q):
        recs = sorted(by_q[q], key=lambda r: r.get("ts", ""))
        for stage in _DIFF_STAGES:
            fn = col_fn[stage]
            per_round = [str(fn(r, r.get("extras") or {})) for r in recs]
            distinct = len(set(per_round))
            if distinct <= 1:
                continue  # 고정 단계는 생략 — 흔들린 것만
            row = [q, stage, distinct] + [_truncate(v) for v in per_round]
            # 회차 셀이 빈 칸이면 패딩
            row += [""] * (max_rounds - len(per_round))
            wf.append(row)
            excel_row = wf.max_row
            for j in range(len(per_round)):
                wf.cell(row=excel_row, column=4 + j).fill = _FLAP_FILL

    # 열 너비 살짝 정리
    for sheet in (ws, wd, wf):
        for col in range(1, sheet.max_column + 1):
            sheet.column_dimensions[get_column_letter(col)].width = 22
    return wb


def main() -> None:
    if len(sys.argv) > 1:
        jsonl_path = Path(sys.argv[1])
    else:
        try:
            from app.core.config import Config

            trace_dir = getattr(Config, "RESPONSE_TRACE_DIR", "log/response_trace")
        except Exception:
            trace_dir = "log/response_trace"
        jsonl_path = Path(trace_dir) / "response_trace.jsonl"

    if not jsonl_path.exists():
        print(f"[error] 트레이스 파일 없음: {jsonl_path}")
        print("  RESPONSE_TRACE_ENABLED=True 로 서버를 띄우고 질문을 호출한 뒤 다시 실행하세요.")
        sys.exit(1)

    records = load_records(jsonl_path)
    if not records:
        print(f"[error] 레코드 0건: {jsonl_path}")
        sys.exit(1)
    records = assign_rounds(records)

    if len(sys.argv) > 2:
        out_path = Path(sys.argv[2])
    else:
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = jsonl_path.parent / f"stage_consistency_{ts}.xlsx"

    wb = build_workbook(records)
    wb.save(out_path)
    n_q = len({(r.get("user_question") or "").strip() for r in records})
    print(f"[done] {len(records)}건 / 질문 {n_q}종 → {out_path}")


if __name__ == "__main__":
    main()
