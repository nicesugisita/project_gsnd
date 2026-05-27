"""일관성·품질 점검표 생성 — gsnd_consistency_*.jsonl → 점검표 xlsx.

1행 = 1문항으로 요약:
① 자동 일관성(개수/검색문서셋/intent/정책태그/reformed/응답텍스트),
② reference-free 품질(gold 불필요·결정적):
   - Groundedness(충실도): 생성 사업명·사실이 검색문서(top_docs)에 근거 있나 → 환각 탐지
   - 무관문서율: 보여준 문서 중 주제어 hit 0 비율 (variable_count.topical_hit 재사용)
③ baseline(과거 스냅샷) 대비 변화: 정확도 아님, 회귀/변화 triage용
④ 수동 품질 체크(드롭다운) → gold 점진 축적

※ golden set 부재. 자동 지표는 환각·무관(품질 하한선)만 잡고, 완전성·적절성은 수동 평가로.

사용:
    python tests/build_review_checklist.py <jsonl> [출력_xlsx] [데이터셋_xlsx]
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, List, Tuple

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from app.chat.infra.rag.variable_count import extract_topic_terms, topical_hit  # noqa: E402

DEFAULT_DATASET = ROOT / "qa_test" / "260513_gsnd_total_dataset_v0.2.xlsx"

# 경남 시군 — 무관문서율의 주제어에서 지역명을 빼기 위함(지역만 겹쳐 hit 되는 것 방지).
GYEONGNAM_SIGUN = (
    "경상남도", "경남", "창원", "진주", "통영", "사천", "김해", "밀양", "거제", "양산",
    "의령", "함안", "창녕", "고성", "남해", "하동", "산청", "함양", "거창", "합천",
)

_DOC_NAME_KEYS = ("BUSINESS_NAME", "SERVICE_NAME", "NAME", "name", "title")
_CELL_MAX = 4000

GREEN = PatternFill("solid", fgColor="C6EFCE")
YELLOW = PatternFill("solid", fgColor="FFEB9C")
RED = PatternFill("solid", fgColor="FFC7CE")
GREY = PatternFill("solid", fgColor="E7E6E6")
HDR = PatternFill("solid", fgColor="DDEBF7")
HDR_FONT = Font(bold=True)
WRAP = Alignment(wrap_text=True, vertical="top")

_SVC_RE = re.compile(r"사업명\s*[:：]\s*(.+)")
_FACT_RE = re.compile(r"\d[\d,\.]*\s*(?:원|만원|천원|억원|억|세|년|개월|개|일|%|퍼센트|회|명|시간|등급|급)")


def _norm(s: str) -> str:
    # 공백 + 천단위 콤마 제거(820,556 == 820556), 소문자화
    return re.sub(r"[\s,]+", "", s or "").casefold()


# 생성 사업명 앞에 붙는 "2026년 (경상남도) 창원시 " 같은 연도·지역 접두어 (시/군/구 접미 포함)
_LOC_PREFIX_RE = re.compile(
    r"^\s*(?:20\d\d\s*년\s*)?(?:경상남도|경남)?\s*"
    r"(?:" + "|".join(s for s in GYEONGNAM_SIGUN if s not in ("경상남도", "경남")) + r")?(?:시|군|구)?\s*"
)


def _canon(name: str) -> str:
    """사업명 정규 키 — 연도·지역 접두어 제거 + 공백/콤마 제거. gold 매칭·근거대조 공용."""
    core = _LOC_PREFIX_RE.sub("", name or "").strip()
    return _norm(core) if len(core) >= 2 else _norm(name)


def _name_grounded(name: str, blob: str) -> bool:
    """생성 사업명이 검색문서에 근거 있는지 — 연도·지역 접두어 제거 후 대조(거짓양성 완화)."""
    return any(c and c in blob for c in {_norm(name), _canon(name)})


def _service_names(text: str) -> List[str]:
    out, seen = [], set()
    for m in _SVC_RE.finditer(text or ""):
        nm = m.group(1).strip().strip("'\"")
        n = _norm(nm)
        if nm and n not in seen:
            seen.add(n)
            out.append(nm)
    return out


def _key_facts(text: str) -> List[str]:
    out, seen = [], set()
    for m in _FACT_RE.finditer(text or ""):
        f = re.sub(r"\s+", "", m.group(0))
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def _docs_blob(top_docs: List[Dict[str, Any]]) -> str:
    """검색문서 전체를 한 덩어리 정규화 텍스트로 (근거 존재 확인용)."""
    parts: List[str] = []
    for d in top_docs or []:
        if isinstance(d, dict):
            for v in d.values():
                if isinstance(v, (str, int, float)):
                    parts.append(str(v))
    return _norm(" ".join(parts))


def _load(path: Path) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _load_baseline(path: Path) -> Dict[int, str]:
    if not path.exists():
        return {}
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["gsnd_total"]
    out: Dict[int, str] = {}
    for i, r in enumerate(ws.iter_rows(values_only=True)):
        if i == 0 or not r or r[0] is None:
            continue
        try:
            no = int(r[0])
        except (TypeError, ValueError):
            continue
        if len(r) > 4 and r[4] is not None:
            out[no] = str(r[4])
    return out


def _doc_names(rec: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for d in rec.get("top_docs") or []:
        if not isinstance(d, dict):
            continue
        nm = next((str(d[k]) for k in _DOC_NAME_KEYS if d.get(k)), None)
        if nm:
            names.append(nm.strip())
    return names


def _avg_jaccard(sets: List[frozenset]) -> float:
    pairs = list(combinations(sets, 2))
    if not pairs:
        return 1.0
    vals = [(len(a & b) / len(a | b) if (a | b) else 1.0) for a, b in pairs]
    return sum(vals) / len(vals)


def _trunc(s: str) -> str:
    s = s or ""
    return s if len(s) <= _CELL_MAX else s[:_CELL_MAX] + f"...[+{len(s) - _CELL_MAX}]"


def _groundedness(gen_text: str, top_docs: List[Dict[str, Any]]) -> Tuple[Any, List[str], str]:
    """생성 항목(사업명 우선, 없으면 사실) 중 검색문서에 근거 있는 비율 + 미근거(환각) 목록."""
    blob = _docs_blob(top_docs)
    names = _service_names(gen_text)
    if names:
        hall = [nm for nm in names if not _name_grounded(nm, blob)]
        return (len(names) - len(hall)) / len(names), hall, "사업명"
    facts = _key_facts(gen_text)
    if facts:
        hall = [f for f in facts if _norm(f) not in blob]
        return (len(facts) - len(hall)) / len(facts), hall, "사실"
    return None, [], "없음"


def _offtopic_ratio(rec: Dict[str, Any]) -> Any:
    """보여준 top_docs 중 주제어 hit 0 비율. 주제어 없으면(광역) None."""
    ex = rec.get("extras") or {}
    topic = extract_topic_terms(ex.get("keywords") or [], exclude=GYEONGNAM_SIGUN)
    docs = rec.get("top_docs") or []
    if not topic or not docs:
        return None
    off = sum(1 for d in docs if isinstance(d, dict) and topical_hit(d, topic) == 0)
    return off / len(docs)


# (헤더, 너비, 종류)
_COLUMNS = [
    ("no", 6, "auto"), ("질문", 34, "auto"), ("회차", 6, "auto"),
    ("문서수(회차별)", 13, "auto"), ("개수 일관성", 10, "auto"),
    ("검색문서셋 일관성", 14, "auto"), ("intent 일관성", 14, "auto"),
    ("정책태그 일관성", 15, "auto"), ("reformed 일관성", 12, "auto"),
    ("응답텍스트 일관성", 16, "auto"),
    ("무관문서율", 11, "qual"),
    ("baseline유형", 10, "base"), ("baseline유지율", 12, "base"),
    ("빠짐(baseline-only,검토)", 24, "base"), ("추가(생성-only,신규)", 24, "base"),
    ("답변정확도", 11, "manual"), ("무관문서(확인)", 12, "manual"),
    ("정보누락", 10, "manual"), ("개수적절성", 11, "manual"), ("비고", 22, "manual"),
    ("대표답변(R1)", 50, "ref"), ("baseline답변", 50, "ref"), ("검색문서(R1)", 42, "ref"),
]
_DROPDOWNS = {
    "답변정확도": '"O,△,X"',
    "무관문서(확인)": '"없음,일부,많음"',
    "정보누락": '"없음,일부,많음"',
    "개수적절성": '"적절,과다,부족"',
}


# baseline-diff 그룹(과거 스냅샷 대비 — 정확도/일관성/품질 아님, triage용). 보고용은 토글로 제외.
_BASELINE_COLS = frozenset({
    "baseline유형", "baseline유지율", "빠짐(baseline-only,검토)", "추가(생성-only,신규)", "baseline답변",
})


def _pct(x) -> str:
    return f"{x * 100:.0f}%" if x is not None else "—"


def build(records: List[Dict[str, Any]], baseline: Dict[int, str], out_path: Path,
          include_baseline: bool = True) -> None:
    cols = [c for c in _COLUMNS if include_baseline or c[0] not in _BASELINE_COLS]
    cidx = {c[0]: i + 1 for i, c in enumerate(cols)}  # 컬럼명 → 1-base 인덱스(필터 반영)
    ncol = len(cols)

    by_q: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in records:
        by_q[(r.get("user_question") or "").strip()].append(r)

    wb = Workbook()
    ws = wb.active
    ws.title = "점검표"
    ws.append([c[0] for c in cols])
    for j, c in enumerate(cols, 1):
        cell = ws.cell(row=1, column=j)
        cell.font = HDR_FONT
        cell.fill = HDR
        cell.alignment = WRAP
        ws.column_dimensions[get_column_letter(j)].width = c[1]

    rownum = 1
    for q in sorted(by_q):
        recs = sorted(by_q[q], key=lambda r: (r.get("_round", 0), r.get("ts", "")))
        n = len(recs)
        no = recs[0].get("_gsnd_no", "")

        def _ex(r):
            return r.get("extras") or {}

        counts = [len(r.get("top_docs") or []) for r in recs]
        name_sets = [frozenset(_doc_names(r)) for r in recs]
        intents = [str(_ex(r).get("intent") or r.get("intent") or "") for r in recs]
        tags = [str(_ex(r).get("policy_priority_tag")) for r in recs]
        reformeds = [str(_ex(r).get("reformed_query") or "") for r in recs]
        resps = [r.get("response_text") or "" for r in recs]
        multi = n >= 2

        # ── 자동 일관성 ──
        if not multi:
            set_v = ("— (1회차)", GREY); int_v = (intents[0], GREY)
            tag_v = (tags[0], GREY); ref_v = ("— (1회차)", GREY); cnt_fill = GREY
        else:
            cnt_fill = GREEN if len(set(counts)) == 1 else RED
            set_fix = len(set(name_sets)) == 1
            set_v = ("✓ 동일" if set_fix else f"⚠ 유사 {_avg_jaccard(name_sets) * 100:.0f}%", GREEN if set_fix else RED)
            int_fix = len(set(intents)) == 1
            int_v = (("✓ " + intents[0]) if int_fix else "⚠ " + ", ".join(sorted(set(intents))), GREEN if int_fix else RED)
            tag_fix = len(set(tags)) == 1
            tag_v = (("✓ " + tags[0]) if tag_fix else "⚠ " + ", ".join(sorted(set(tags))), GREEN if tag_fix else RED)
            ref_fix = len(set(reformeds)) == 1
            ref_v = ("✓ 동일" if ref_fix else f"⚠ {len(set(reformeds))}종", GREEN if ref_fix else RED)
        cnt_str = ("✓ " if (multi and len(set(counts)) == 1) else ("⚠ " if multi else "")) + " / ".join(map(str, counts))
        distinct_resp = len(set(resps)); lens = [len(t) for t in resps]
        if not multi:
            resp_v = (f"len {lens[0] if lens else 0}", GREY)
        elif distinct_resp == 1:
            resp_v = ("✓ 완전동일", GREEN)
        else:
            resp_v = (f"⚠ {distinct_resp}종 (len {min(lens)}~{max(lens)})", YELLOW)

        # ── 품질(reference-free, R1) ──
        gen_r1 = resps[0] if resps else ""
        ground, hall, _gkind = _groundedness(gen_r1, recs[0].get("top_docs") or [])
        if ground is None:
            ground_fill = GREY
        elif ground >= 0.999:
            ground_fill = GREEN
        elif ground >= 0.8:
            ground_fill = YELLOW
        else:
            ground_fill = RED
        off = _offtopic_ratio(recs[0])
        if off is None:
            off_fill = GREY
        elif off <= 0.0:
            off_fill = GREEN
        elif off <= 0.34:
            off_fill = YELLOW
        else:
            off_fill = RED

        # ── baseline 대비 (R1) ──
        base_ans = baseline.get(no, "") if isinstance(no, int) else ""
        b_names = _service_names(base_ans)
        if b_names:
            g_names = _service_names(gen_r1)
            bset = {_norm(x): x for x in b_names}; gset = {_norm(x): x for x in g_names}
            common = set(bset) & set(gset)
            keep = len(common) / len(bset) if bset else None
            miss = [bset[k] for k in bset if k not in gset]
            add = [gset[k] for k in gset if k not in bset]
            btype = "카드"
        elif _key_facts(base_ans):
            facts = _key_facts(base_ans); gnorm = _norm(gen_r1)
            keep = sum(1 for f in facts if _norm(f) in gnorm) / len(facts) if facts else None
            miss = [f for f in facts if _norm(f) not in gnorm]; add = []
            btype = "산문(사실)"
        else:
            keep = None; miss = []; add = []; btype = "없음" if not base_ans else "기타"
        keep_fill = GREY if keep is None else (GREEN if keep >= 0.999 else YELLOW)

        vals = {
            "no": no, "질문": q, "회차": n,
            "문서수(회차별)": cnt_str,
            "개수 일관성": "✓" if (multi and len(set(counts)) == 1) else ("⚠" if multi else "—"),
            "검색문서셋 일관성": set_v[0], "intent 일관성": int_v[0],
            "정책태그 일관성": tag_v[0], "reformed 일관성": ref_v[0], "응답텍스트 일관성": resp_v[0],
            "충실도(근거율)": _pct(ground), "환각(미근거)": ", ".join(hall), "무관문서율": _pct(off),
            "baseline유형": btype, "baseline유지율": _pct(keep),
            "빠짐(baseline-only,검토)": ", ".join(miss), "추가(생성-only,신규)": ", ".join(add),
            "답변정확도": "", "무관문서(확인)": "", "정보누락": "", "개수적절성": "", "비고": "",
            "대표답변(R1)": _trunc(gen_r1), "baseline답변": _trunc(base_ans),
            "검색문서(R1)": " | ".join(_doc_names(recs[0])) if recs else "",
        }
        ws.append([vals.get(c[0], "") for c in cols])
        rownum += 1

        _fills = [
            ("개수 일관성", cnt_fill), ("검색문서셋 일관성", set_v[1]), ("intent 일관성", int_v[1]),
            ("정책태그 일관성", tag_v[1]), ("reformed 일관성", ref_v[1]), ("응답텍스트 일관성", resp_v[1]),
            ("충실도(근거율)", ground_fill), ("무관문서율", off_fill),
        ]
        if include_baseline:
            _fills.append(("baseline유지율", keep_fill))
        for title, fill in _fills:
            if title in cidx:
                ws.cell(row=rownum, column=cidx[title]).fill = fill
        if hall and "환각(미근거)" in cidx:
            ws.cell(row=rownum, column=cidx["환각(미근거)"]).fill = RED
        if include_baseline and miss and "빠짐(baseline-only,검토)" in cidx:
            ws.cell(row=rownum, column=cidx["빠짐(baseline-only,검토)"]).fill = YELLOW
        for title in ("질문", "비고", "대표답변(R1)", "baseline답변", "검색문서(R1)",
                      "환각(미근거)", "빠짐(baseline-only,검토)", "추가(생성-only,신규)"):
            if title in cidx:
                ws.cell(row=rownum, column=cidx[title]).alignment = WRAP

    last = rownum
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(ncol)}{last}"
    for title, formula in _DROPDOWNS.items():
        if title not in cidx:
            continue
        col = get_column_letter(cidx[title])
        dv = DataValidation(type="list", formula1=formula, allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"{col}2:{col}{last}")

    # ── 요약 ──
    s = wb.create_sheet("요약", 0)
    s.column_dimensions["A"].width = 28
    s.column_dimensions["B"].width = 14
    s.column_dimensions["C"].width = 12
    total = last - 1
    s.append(["일관성·품질 점검 요약"]); s["A1"].font = Font(bold=True, size=13)
    s.append(["총 문항 수", total]); s.append([])

    def _ratio(label, title):
        col = get_column_letter(cidx[title]); rng = f"점검표!{col}2:{col}{last}"
        s.append([label, f'=COUNTIF({rng},"✓*")', f'=IFERROR(COUNTIF({rng},"✓*")/{total},0)'])

    s.append(["[자동] 일관성 ✓개수 / 비율", "✓개수", "비율"])
    s.cell(row=s.max_row, column=1).font = HDR_FONT
    for t in ("개수 일관성", "검색문서셋 일관성", "intent 일관성", "정책태그 일관성", "reformed 일관성", "응답텍스트 일관성"):
        _ratio(t.replace(" 일관성", ""), t)
    for rr in range(5, s.max_row + 1):
        s.cell(row=rr, column=3).number_format = "0%"

    s.append([])
    s.append(["[품질·reference-free] gold 불필요·결정적", "", ""])
    s.cell(row=s.max_row, column=1).font = HDR_FONT
    ocol = get_column_letter(cidx["무관문서율"])
    s.append(["평균 무관문서율", f'=IFERROR(AVERAGEIF(점검표!{ocol}2:{ocol}{last},"<>—"),"")'])
    s.cell(row=s.max_row, column=2).number_format = "0%"

    s.append([])
    s.append(["[수동] 답변정확도 (채우면 자동 갱신)"])
    s.cell(row=s.max_row, column=1).font = HDR_FONT
    ac = get_column_letter(cidx["답변정확도"]); arng = f"점검표!{ac}2:{ac}{last}"
    s.append(["O (정확)", f'=COUNTIF({arng},"O")'])
    s.append(["△ (부분)", f'=COUNTIF({arng},"△")'])
    s.append(["X (오류)", f'=COUNTIF({arng},"X")'])
    s.append(["미평가", f'=COUNTBLANK({arng})'])
    s.append([])
    s.append(["범례", "무관율: 0%초록 / ≤34%노랑 / 초과빨강 · 일관성: ✓고정(초록) / ⚠흔들림(빨강) / 회색=1회차"])
    _note = "무관율은 무관 문서 섞임(정밀도 하한선)만 측정 — 완전성·적절성은 수동평가로."
    if include_baseline:
        _note += " baseline은 과거스냅샷(정확도 아님)."
    s.append(["주의", _note])

    wb.save(out_path)
    print(f"[done] 문항 {total}종 → {out_path}")


def main() -> None:
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    include_baseline = not ({"--no-baseline", "--report"} & flags)
    if not args:
        print("usage: python tests/build_review_checklist.py <jsonl> [out_xlsx] [dataset_xlsx] [--report|--no-baseline]")
        sys.exit(1)
    jsonl = Path(args[0])
    if not jsonl.exists():
        print(f"[error] 입력 없음: {jsonl}"); sys.exit(1)
    suffix = "_checklist.xlsx" if include_baseline else "_checklist_report.xlsx"
    out = Path(args[1]) if len(args) > 1 else jsonl.with_name(jsonl.stem + suffix)
    dataset = Path(args[2]) if len(args) > 2 else DEFAULT_DATASET
    records = _load(jsonl)
    if not records:
        print(f"[error] 레코드 0건: {jsonl}"); sys.exit(1)
    if include_baseline:
        baseline = _load_baseline(dataset)
        print(f"[info] baseline 로드: {len(baseline)}건 ({dataset.name})")
    else:
        baseline = {}
        print("[info] --report/--no-baseline: baseline 그룹 제외(보고용)")
    build(records, baseline, out, include_baseline=include_baseline)


if __name__ == "__main__":
    main()
