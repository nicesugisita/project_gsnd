"""답변 일관성 + 품질 비교 분석 — answer_v2(stages) + goldenset(answer_doc) → 분석 xlsx.

입력:
- answer_v2 xlsx: stages 시트(회차별 단계 데이터). 질문별 여러 회차 → 단계별 일관성.
- gold xlsx: gold 시트의 answer_doc(사람이 확정한 기대 사업명) → 품질 P/R/F1 기준.

출력(1행=1문항):
- [일관성] intent / reformed / keywords / 정책태그 / 검색문서셋 / 문서수 / 응답텍스트  (회차 간 ✓고정·⚠흔들림)
- [품질] gold 매칭 문항만: 검색문서(R1) vs answer_doc → Precision / Recall / F1 + 빠짐(미검출) / 잘못(무관후보)

사용:
    python tests/analyze_answer_quality.py <answer_v2.xlsx> <gold.xlsx> [out.xlsx]
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from itertools import combinations
from pathlib import Path
from typing import Any, Dict, List, Set

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_review_checklist import _canon, _norm  # noqa: E402

GREEN = PatternFill("solid", fgColor="C6EFCE")
YELLOW = PatternFill("solid", fgColor="FFEB9C")
RED = PatternFill("solid", fgColor="FFC7CE")
GREY = PatternFill("solid", fgColor="E7E6E6")
HDR = PatternFill("solid", fgColor="DDEBF7")
HDR_FONT = Font(bold=True)
WRAP = Alignment(wrap_text=True, vertical="top")
_CELL_MAX = 4000

# answer_v2 stages 컬럼 인덱스 (0-base)
C_Q, C_TS, C_ROUND = 0, 1, 2
C_INTENT, C_REFORMED, C_KEYWORDS, C_TAG = 3, 4, 6, 7
C_DOCS, C_NDOCS, C_RESP = 14, 15, 17


def _trunc(s: str) -> str:
    s = str(s or "")
    return s if len(s) <= _CELL_MAX else s[:_CELL_MAX] + f"...[+{len(s) - _CELL_MAX}]"


def _split_docs(cell: Any) -> List[str]:
    return [x.strip() for x in str(cell or "").split("‖") if x.strip()]


def _gold_names(cell: Any) -> List[str]:
    return [x.strip() for x in str(cell or "").splitlines() if x.strip()]


def _avg_jac(sets: List[frozenset]) -> float:
    pairs = list(combinations(sets, 2))
    if not pairs:
        return 1.0
    return sum((len(a & b) / len(a | b) if (a | b) else 1.0) for a, b in pairs) / len(pairs)


def _load_gold(path: Path) -> Dict[str, List[str]]:
    """norm(question) → answer_doc 사업명 리스트 (채워진 것만)."""
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb["gold"]
    hdr = [str(c.value or "") for c in ws[1]]
    qi = next((i for i, h in enumerate(hdr) if "question" in h), 1)
    ai = next((i for i, h in enumerate(hdr) if "answer_doc" in h), 2)
    out: Dict[str, List[str]] = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r or r[qi] is None:
            continue
        names = _gold_names(r[ai])
        if names:
            out[_norm(r[qi])] = names
    return out


def _load_stages(path: Path) -> Dict[str, List[tuple]]:
    """norm(question) → [회차 행, ...] (round 오름차순)."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["stages"]
    by_q: Dict[str, List[tuple]] = defaultdict(list)
    qdisp: Dict[str, str] = {}
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r or r[C_Q] is None:
            continue
        k = _norm(r[C_Q])
        qdisp.setdefault(k, str(r[C_Q]))
        by_q[k].append(r)
    for k in by_q:
        by_q[k].sort(key=lambda x: (x[C_ROUND] if isinstance(x[C_ROUND], int) else 0, str(x[C_TS] or "")))
    return by_q, qdisp


_COLS = [
    ("질문", 36), ("회차", 6),
    ("intent", 9), ("reformed", 10), ("keywords", 10), ("정책태그", 10),
    ("검색문서셋", 12), ("문서수", 9), ("응답텍스트", 12),
    ("gold有", 7), ("검색문서수(R1)", 11), ("Precision", 10), ("Recall", 9), ("F1", 8),
    ("빠짐(gold-only,미검출)", 28), ("잘못(docs-only,무관후보)", 28),
    ("검색문서(R1)", 46), ("최종응답(R1)", 46),
]


def main() -> None:
    if len(sys.argv) < 3:
        print("usage: python tests/analyze_answer_quality.py <answer_v2.xlsx> <gold.xlsx> [out.xlsx]")
        sys.exit(1)
    ans_p, gold_p = Path(sys.argv[1]), Path(sys.argv[2])
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else ans_p.with_name(ans_p.stem + "_quality.xlsx")
    gold = _load_gold(gold_p)
    by_q, qdisp = _load_stages(ans_p)
    print(f"[info] answer_v2 질문 {len(by_q)}종 | gold answer_doc {len(gold)}종 | 매칭 {len(set(by_q) & set(gold))}종")

    wb = Workbook()
    ws = wb.active
    ws.title = "분석"
    ws.append([c[0] for c in _COLS])
    for j, c in enumerate(_COLS, 1):
        cell = ws.cell(row=1, column=j)
        cell.font = HDR_FONT; cell.fill = HDR; cell.alignment = WRAP
        ws.column_dimensions[get_column_letter(j)].width = c[1]
    cidx = {c[0]: i + 1 for i, c in enumerate(_COLS)}

    def _fix(vals) -> tuple:
        s = set(vals)
        if len(s) == 1:
            return "✓", GREEN
        return f"⚠ {len(s)}종", RED

    sumP = sumR = sumF = 0.0
    nQ = 0
    fix_counts = defaultdict(int)
    rownum = 1
    for k in sorted(by_q, key=lambda x: qdisp[x]):
        recs = by_q[k]
        n = len(recs)
        multi = n >= 2
        q = qdisp[k]

        def col(ci):
            return [str(r[ci] or "") for r in recs]

        # 단계별 일관성
        consist = {}
        for label, ci in (("intent", C_INTENT), ("reformed", C_REFORMED),
                          ("keywords", C_KEYWORDS), ("정책태그", C_TAG),
                          ("문서수", C_NDOCS), ("응답텍스트", C_RESP)):
            if not multi:
                consist[label] = ("— (1회차)", GREY)
            else:
                consist[label] = _fix(col(ci))
        # 검색문서셋 (Jaccard)
        if not multi:
            consist["검색문서셋"] = ("— (1회차)", GREY)
        else:
            dsets = [frozenset(_canon(x) for x in _split_docs(r[C_DOCS])) for r in recs]
            if len(set(dsets)) == 1:
                consist["검색문서셋"] = ("✓", GREEN)
            else:
                consist["검색문서셋"] = (f"⚠ 유사 {_avg_jac(dsets) * 100:.0f}%", RED)
        for label, (txt, _f) in consist.items():
            if txt.startswith("✓"):
                fix_counts[label] += 1

        # 품질 (R1 검색문서 vs gold)
        r1 = recs[0]
        gen_disp = _split_docs(r1[C_DOCS])
        gen = {_canon(x) for x in gen_disp if _canon(x)}
        gnames = gold.get(k)
        if gnames:
            gset = {_canon(x): x for x in gnames if _canon(x)}
            inter = gen & set(gset)
            P = len(inter) / len(gen) if gen else 0.0
            R = len(inter) / len(gset) if gset else 0.0
            F = 2 * P * R / (P + R) if (P + R) else 0.0
            miss = [gset[c] for c in gset if c not in gen]
            wrong = [d for d in gen_disp if _canon(d) not in gset]
            sumP += P; sumR += R; sumF += F; nQ += 1
            gold_y, pcol = "Y", (lambda v: GREEN if v >= 0.8 else (YELLOW if v >= 0.5 else RED))
            qvals = [len(gen), round(P, 2), round(R, 2), round(F, 2), " | ".join(miss), " | ".join(wrong)]
        else:
            gold_y, pcol = "N", None
            qvals = [len(gen), "", "", "", "", ""]

        vals = {
            "질문": q, "회차": n,
            "intent": consist["intent"][0], "reformed": consist["reformed"][0],
            "keywords": consist["keywords"][0], "정책태그": consist["정책태그"][0],
            "검색문서셋": consist["검색문서셋"][0], "문서수": consist["문서수"][0],
            "응답텍스트": consist["응답텍스트"][0],
            "gold有": gold_y, "검색문서수(R1)": qvals[0],
            "Precision": qvals[1], "Recall": qvals[2], "F1": qvals[3],
            "빠짐(gold-only,미검출)": qvals[4], "잘못(docs-only,무관후보)": qvals[5],
            "검색문서(R1)": _trunc(" | ".join(gen_disp)), "최종응답(R1)": _trunc(r1[C_RESP]),
        }
        ws.append([vals[c[0]] for c in _COLS])
        rownum += 1
        # 일관성 색
        for label in ("intent", "reformed", "keywords", "정책태그", "검색문서셋", "문서수", "응답텍스트"):
            ws.cell(row=rownum, column=cidx[label]).fill = consist[label][1]
        # 품질 색
        if pcol:
            for label, v in (("Precision", P), ("Recall", R), ("F1", F)):
                ws.cell(row=rownum, column=cidx[label]).fill = pcol(v)
        for label in ("질문", "빠짐(gold-only,미검출)", "잘못(docs-only,무관후보)", "검색문서(R1)", "최종응답(R1)"):
            ws.cell(row=rownum, column=cidx[label]).alignment = WRAP

    last = rownum
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(_COLS))}{last}"

    # 요약
    s = wb.create_sheet("요약", 0)
    s.column_dimensions["A"].width = 24; s.column_dimensions["B"].width = 14; s.column_dimensions["C"].width = 12
    total = last - 1
    s.append(["답변 일관성·품질 분석 요약"]); s["A1"].font = Font(bold=True, size=13)
    s.append(["총 문항(일관성)", total])
    s.append(["품질 측정(gold 매칭)", nQ]); s.append([])
    s.append(["[단계별 일관성] ✓고정 / 비율"]); s.cell(row=s.max_row, column=1).font = HDR_FONT
    for label in ("intent", "reformed", "keywords", "정책태그", "검색문서셋", "문서수", "응답텍스트"):
        s.append([label, fix_counts[label], (fix_counts[label] / total if total else 0)])
        s.cell(row=s.max_row, column=3).number_format = "0%"
    s.append([])
    s.append(["[품질 vs gold] Macro 평균 (gold 매칭 문항)"]); s.cell(row=s.max_row, column=1).font = HDR_FONT
    if nQ:
        s.append(["Precision", round(sumP / nQ, 3)])
        s.append(["Recall", round(sumR / nQ, 3)])
        s.append(["F1", round(sumF / nQ, 3)])
    s.append([])
    s.append(["범례", "일관성 ✓고정(초록)/⚠흔들림(빨강) · 품질 ≥0.8초록/≥0.5노랑/<0.5빨강 · Precision=정확(무관無), Recall=많이(관련 다)"])

    wb.save(out)
    print(f"[done] 분석 {total}문항(품질 {nQ}문항) → {out}")
    if nQ:
        print(f"  품질 Macro  Precision={sumP/nQ:.3f}  Recall={sumR/nQ:.3f}  F1={sumF/nQ:.3f}")


if __name__ == "__main__":
    main()
