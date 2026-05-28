"""답변 실행본 vs 정답셋(gold) 비교 — 260527_vs_0515_compare_v1.xlsx 형식 재현.

ref  = 정답셋 = 260513_gsnd_total_dataset_v0.2.xlsx 의 final_answer
curr = 모델답변 = 260528_answer_v1.xlsx 의 stages 시트, round==1 응답

지표: Jaccard = |교집합|/|합집합| (사업명 canon set). 양쪽 다 서비스 0개면 None.
status: structured([서비스]) / narrative(서술) / empty(공백) / error(curr_error).
flags: status_diff(ref->curr) / low_jaccard(<0.5) / no_services_extracted 콤마결합.

시트:
  Summary  — 항목/값 집계
  Compare  — 공통 질문 전체(교집합)
  Diff     — flags 있는 행만(차이 있는 행)
  P1_error / P2_jaccard_zero / P3_struct_to_narr / P4_narr_to_struct — 우선순위 triage

사용: python tests/compare_answer_vs_gold.py [answer_xlsx] [gold_xlsx] [out_xlsx]
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path
from statistics import mean

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_review_checklist import _service_names, _canon  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEF_ANS = ROOT / "qa_test" / "260528_answer_v1.xlsx"
DEF_GOLD = ROOT / "qa_test" / "260513_gsnd_total_dataset_v0.2.xlsx"
DEF_OUT = ROOT / "qa_test" / "260528_vs_gold_compare_v1.xlsx"
USE_ROUND = 1

COLS = [
    ("no", 6), ("question", 38), ("ref_status", 11), ("curr_status", 11),
    ("ref_service_count", 9), ("curr_service_count", 9), ("common_count", 9),
    ("only_ref_count", 9), ("only_curr_count", 9), ("jaccard", 8),
    ("common_services", 30), ("only_ref_services", 30), ("only_curr_services", 30),
    ("len_ref", 8), ("len_curr", 8), ("len_ratio_curr_over_ref", 12),
    ("flags", 26), ("검토_판정", 16), ("검토_메모", 60),
    ("ref_answer", 60), ("curr_answer", 60), ("curr_error", 14),
]
# 검토_판정 색상
VERDICT_FILL = {
    "오류": PatternFill("solid", fgColor="C00000"),
    "완전불일치": PatternFill("solid", fgColor="F8CBAD"),
    "심각불일치": PatternFill("solid", fgColor="FCE4D6"),
    "미흡": PatternFill("solid", fgColor="FFF2CC"),
    "부분일치": PatternFill("solid", fgColor="FFF7E0"),
    "양호": PatternFill("solid", fgColor="E2EFDA"),
    "완전일치": PatternFill("solid", fgColor="C6E0B4"),
}
HDR_FILL = PatternFill("solid", fgColor="DDEBF7")
TITLE_FILL = PatternFill("solid", fgColor="FCE4D6")
ZERO_FILL = PatternFill("solid", fgColor="F8CBAD")   # jaccard 0
LOW_FILL = PatternFill("solid", fgColor="FFF7E0")    # low jaccard
BOLD = Font(bold=True)
WRAP = Alignment(wrap_text=True, vertical="top")


def classify(t: str) -> str:
    t = (t or "").strip()
    if not t:
        return "empty"
    if "[서비스" in t:
        return "structured"
    return "narrative"


def svc_set(t: str):
    out = {}
    for nm in _service_names(t):
        c = _canon(nm)
        if c and c not in out:
            out[c] = nm
    return out  # canon -> display


def load_gold(path: Path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["gsnd_total"]
    gold, no_of = {}, {}
    for i, r in enumerate(ws.iter_rows(values_only=True)):
        if i == 0 or not r or r[1] is None:
            continue
        q = str(r[1]).strip()
        gold[q] = str(r[4]).strip() if len(r) > 4 and r[4] else ""
        try:
            no_of[q] = int(r[0])
        except (TypeError, ValueError):
            no_of[q] = None
    return gold, no_of


def load_model(path: Path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["stages"]
    model, err = {}, {}
    hdr = [c.value for c in next(ws.iter_rows(max_row=1))]
    ei = hdr.index("error") if "error" in hdr else None
    for i, r in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue
        if r[2] == USE_ROUND:
            q = str(r[0]).strip()
            model[q] = str(r[-1]).strip() if r[-1] else ""
            if ei is not None and r[ei]:
                err[q] = str(r[ei])
    return model, err


def build_row(no, q, ref, curr, curr_err):
    rs, cs = classify(ref), classify(curr)
    if curr_err:
        cs = "error"
    rset, cset = svc_set(ref), svc_set(curr)
    rk, ck = set(rset), set(cset)
    inter = rk & ck
    only_r = rk - ck
    only_c = ck - rk
    union = rk | ck
    jac = (len(inter) / len(union)) if union else None

    flags = []
    if curr_err:
        flags.append("error")
    if rs != cs:
        flags.append(f"status_diff({rs}->{cs})")
    if jac is None:
        flags.append("no_services_extracted")
    elif jac < 0.5:
        flags.append("low_jaccard")
    len_r, len_c = len(ref), len(curr)
    ratio = round(len_c / len_r, 4) if len_r else ""

    verdict, memo = _verdict(rs, cs, jac, len(inter), len(only_r), len(only_c),
                             len(rk), len(ck), curr_err)

    return {
        "no": no, "question": q, "ref_status": rs, "curr_status": cs,
        "ref_service_count": len(rk), "curr_service_count": len(ck),
        "common_count": len(inter), "only_ref_count": len(only_r),
        "only_curr_count": len(only_c),
        "jaccard": round(jac, 4) if jac is not None else "",
        "_jac": jac,
        "common_services": "\n".join(rset[c] for c in inter),
        "only_ref_services": "\n".join(rset[c] for c in only_r),
        "only_curr_services": "\n".join(cset[c] for c in only_c),
        "len_ref": len_r, "len_curr": len_c, "len_ratio_curr_over_ref": ratio,
        "flags": ",".join(flags), "검토_판정": verdict, "검토_메모": memo,
        "ref_answer": ref, "curr_answer": curr, "curr_error": curr_err or "",
    }


def _verdict(rs, cs, jac, common, only_r, only_c, n_ref, n_curr, curr_err):
    """질문에 대한 답변 결과를 자동 판정 + 한줄 메모. set 채점 불가는 정성검토 표시."""
    if curr_err:
        return "오류", f"현재 답변이 오류로 종료 — 백엔드 로그·재현 확인 필요({str(curr_err)[:40]})."
    if rs == "empty":
        return "정답공백(검증불가)", f"정답셋에 답변이 없음 → 모델은 {n_curr}건 추천. 정답셋 보강 후 재채점 필요."
    if rs == "structured" and cs == "narrative":
        return "형식회귀", f"정답은 추천리스트({n_ref}건)인데 현재는 서술체로 축소 — 추천 누락 의심. 정성검토."
    if rs == "narrative" and cs == "structured":
        return "형식확장(검증불가)", f"정답은 서술형 단일안내인데 현재는 {n_curr}건 리스트로 확장 — 과생성 여부 정성검토."
    if rs == "narrative" and cs == "narrative":
        return "서술형(정성검토)", "양쪽 서술형 단일안내 — 사업명 set 채점 불가, 핵심 사업·금액 일치 여부 정성검토."
    # 양쪽 structured → set 채점
    if jac is None:
        return "서술형(정성검토)", "서비스 미추출 — 정성검토."
    base = f"정답 {n_ref}건 중 {common}건 일치(jaccard {jac:.2f}), 누락 {only_r} / 추가 {only_c}."
    if jac == 0:
        return "완전불일치", base + " 추천 서비스가 정답과 전혀 안 겹침 — 검색·리랭킹 실패(최우선)."
    if jac < 0.34:
        return "심각불일치", base + " 대부분 빗나감 — 검색 관련성 점검 필요."
    if jac < 0.5:
        return "미흡", base
    if jac < 1.0:
        return "부분일치", base
    return "완전일치", base


def _write_table(ws, rows, title=None):
    r0 = 1
    if title:
        cell = ws.cell(row=1, column=1, value=title)
        cell.font = BOLD; cell.fill = TITLE_FILL
        r0 = 2
    keys = [c[0] for c in COLS]
    for j, (name, w) in enumerate(COLS, 1):
        c = ws.cell(row=r0, column=j, value=name)
        c.font = BOLD; c.fill = HDR_FILL; c.alignment = WRAP
        ws.column_dimensions[get_column_letter(j)].width = w
    for di, d in enumerate(rows):
        rr = r0 + 1 + di
        for j, k in enumerate(keys, 1):
            ws.cell(row=rr, column=j, value=d[k])
        for cc in (2, 11, 12, 13, 17, 18, 19, 20, 21):
            ws.cell(row=rr, column=cc).alignment = WRAP
        jac = d["_jac"]
        if jac is not None:
            if jac == 0:
                ws.cell(row=rr, column=10).fill = ZERO_FILL
            elif jac < 0.5:
                ws.cell(row=rr, column=10).fill = LOW_FILL
        vf = VERDICT_FILL.get(d["검토_판정"])
        if vf:
            ws.cell(row=rr, column=18).fill = vf
    last = r0 + len(rows)
    ws.freeze_panes = ws.cell(row=r0 + 1, column=3).coordinate
    if rows:
        ws.auto_filter.ref = f"A{r0}:{get_column_letter(len(COLS))}{last}"


def main() -> None:
    ans_p = Path(sys.argv[1]) if len(sys.argv) > 1 else DEF_ANS
    gold_p = Path(sys.argv[2]) if len(sys.argv) > 2 else DEF_GOLD
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else DEF_OUT

    gold, no_of = load_gold(gold_p)
    model, errs = load_model(ans_p)
    gk, mk = set(gold), set(model)
    common = sorted(gk & mk, key=lambda q: (no_of.get(q) is None, no_of.get(q) or 0))

    rows = [build_row(no_of.get(q), q, gold[q], model[q], errs.get(q)) for q in common]

    wb = Workbook()
    # placeholder; build Summary last so we have stats. Use temp active.
    ws_cmp = wb.active
    ws_cmp.title = "Compare"
    _write_table(ws_cmp, rows)

    diff_rows = [d for d in rows if d["flags"]]
    _write_table(wb.create_sheet("Diff"), diff_rows)

    p1 = [d for d in rows if d["curr_status"] == "error"]
    # P2: 양쪽 모두 서비스 추천(structured) + 교집합 0 — narrative/empty→structured 는 제외
    p2 = [d for d in rows
          if d["_jac"] == 0 and d["ref_status"] == "structured" and d["curr_status"] == "structured"]
    p3 = [d for d in rows if d["ref_status"] == "structured" and d["curr_status"] == "narrative"]
    p4 = [d for d in rows if d["ref_status"] == "narrative" and d["curr_status"] == "structured"]
    _write_table(wb.create_sheet("P1_error"), p1,
                 "[P1] 현재(260528) 답변이 오류로 끝난 케이스 — 가장 먼저 확인. 백엔드 로그·재현 필요.")
    _write_table(wb.create_sheet("P2_jaccard_zero"), p2,
                 "[P2] 양쪽 모두 서비스를 추천했는데 교집합이 0 — 추천 서비스가 완전히 바뀐 케이스.")
    _write_table(wb.create_sheet("P3_struct_to_narr"), p3,
                 "[P3] 정답(gold)은 구조화 답변인데 현재(260528)는 산문체 — 구조화 회귀/누락 의심.")
    _write_table(wb.create_sheet("P4_narr_to_struct"), p4,
                 "[P4] 정답(gold)은 산문체인데 현재(260528)는 구조화 — 확장/개선.")

    # ── Summary ──
    s = wb.create_sheet("Summary", 0)
    s.column_dimensions["A"].width = 34
    s.column_dimensions["B"].width = 16
    s.append(["항목", "값"])
    for c in s[1]:
        c.font = BOLD; c.fill = HDR_FILL
    def line(a, b):
        s.append([a, b])

    trans = Counter((d["ref_status"], d["curr_status"]) for d in rows)
    jvals = [d["_jac"] for d in rows if d["_jac"] is not None]
    n_status_changed = sum(1 for d in rows if d["ref_status"] != d["curr_status"])

    line("총 비교 행수(공통 질문)", len(rows))
    line("current(260528) round 사용", USE_ROUND)
    line("ref(gold) only (current에 없음)", len(gk - mk))
    line("current(260528) only (gold에 없음)", len(mk - gk))
    line("", "")
    for (rsv, csv), n in sorted(trans.items(), key=lambda x: -x[1]):
        line(f"{rsv} -> {csv}", n)
    line("", "")
    line("평균 Jaccard (서비스 추출된 행만)", round(mean(jvals), 3) if jvals else "N/A")
    line("0.5~<1.0", sum(1 for j in jvals if 0.5 <= j < 1.0))
    line("0<j<0.5", sum(1 for j in jvals if 0 < j < 0.5))
    line("N/A(no svc)", sum(1 for d in rows if d["_jac"] is None))
    line("error 행", len(p1))
    line("status 변화 행", n_status_changed)
    for (rsv, csv), n in sorted(trans.items(), key=lambda x: -x[1]):
        if rsv != csv:
            line(f"  ↳ {rsv} -> {csv}", n)
    line("low_jaccard (<0.5) 행", sum(1 for d in rows if d["_jac"] is not None and d["_jac"] < 0.5))
    line("완전 일치(jaccard=1.0)", sum(1 for j in jvals if j >= 0.999))
    line("교집합 0 (jaccard=0, 전체)", sum(1 for j in jvals if j == 0))
    line("  ↳ 양쪽 structured (P2)", len(p2))
    line("", "")
    line("[Diff 행수] flags 있는 행", len(diff_rows))
    line("", "")
    line("■ 검토_판정 분포", "")
    vc = Counter(d["검토_판정"] for d in rows)
    order = ["완전일치", "양호", "부분일치", "미흡", "심각불일치", "완전불일치",
             "형식회귀", "형식확장(검증불가)", "서술형(정성검토)", "정답공백(검증불가)", "오류"]
    for v in order:
        if vc.get(v):
            line(f"  {v}", vc[v])
    for v, n in vc.items():
        if v not in order:
            line(f"  {v}", n)

    wb.save(out)

    # 콘솔
    print(f"[done] {out}")
    print(f"  공통 {len(rows)}문항 | Diff {len(diff_rows)} | P1 {len(p1)} P2 {len(p2)} P3 {len(p3)} P4 {len(p4)}")
    print(f"  평균 Jaccard={mean(jvals):.3f} (서비스추출 {len(jvals)}행) | jaccard=0: {len(p2)}")
    print("  status 전이:", dict(trans))


if __name__ == "__main__":
    main()
