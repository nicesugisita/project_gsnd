"""답변 일관성 분석 — stages 시트(회차 반복 호출)에서 각 단계가 회차 간 얼마나 흔들리나.

같은 질문을 N회 호출했을 때 단계별 산출이 회차마다 같은지(결정적인지) 본다.
ts 가 같은 행은 중복 로그로 보고 제거. 실제 호출 2회 이상인 질문만 비교 대상.

각 단계마다:
- raw_변동: 정규화 텍스트가 회차 간 1종류 초과(달라짐)인 질문 수/비율.
- set_변동(검색·리랭킹·응답 단계만): 사업명 set 이 회차 간 달라진 질문 수.
  → raw 는 변동인데 set 은 동일하면 순서·가중치 jitter, set 까지 변동이면 실제 추천이 바뀐 것.

출력: <stem>_consistency.xlsx (Summary / PerQuestion / 단계별변동 시트), 콘솔 요약.
사용: python tests/analyze_answer_consistency.py [stages_xlsx] [out_xlsx]
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_review_checklist import _service_names, _canon  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEF_IN = ROOT / "qa_test" / "260528_answer_v1.1.xlsx"

# (헤더명, set비교 대상 여부)
STAGES = [
    ("의도분류(intent)", False),
    ("쿼리재구성(reformed)", False),
    ("쿼리리라이팅(expanded)", False),
    ("키워드추출(keywords)", False),
    ("정책태그(policy_tag)", False),
    ("벡터쿼리(vector_query)", False),
    ("마리너 검색쿼리(leg별)", False),
    ("마리너 필터", False),
    ("검색식(WhereSet)", False),
    ("리랭킹전(후보풀)", True),
    ("리랭킹후", True),
    ("검색문서(docs)", True),
    ("최종응답(response)", True),
]
POOL_RE = re.compile(r"\d+\.\s*(.+?)\s*\(W=")
HDR = PatternFill("solid", fgColor="DDEBF7")
WARN = PatternFill("solid", fgColor="FCE4D6")
HOT = PatternFill("solid", fgColor="F8CBAD")
BOLD = Font(bold=True)
WRAP = Alignment(wrap_text=True, vertical="top")


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip())


def svc_set(cell, is_pool):
    """단계 셀에서 사업명 canon set 추출. 포맷 3종 지원:
    - 후보풀/리랭킹: '1. 이름 (W=...)'
    - 검색문서(docs): '이름 ‖ 이름 ‖ ...' (.pdf 문서명 제외)
    - 최종응답: '사업명 :' 필드
    """
    text = str(cell or "")
    if not text or text == "None":
        return frozenset()
    if POOL_RE.search(text):
        names = [m.group(1) for m in POOL_RE.finditer(text)]
    elif "‖" in text:
        names = [t.strip() for t in text.split("‖")]
        names = [t for t in names if t and not t.lower().endswith(".pdf")]
    else:
        names = _service_names(text)
    return frozenset(c for c in (_canon(n) for n in names) if c)


def load(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["stages"]
    rows = list(ws.iter_rows(values_only=True))
    hdr = list(rows[0])
    I = {h: i for i, h in enumerate(hdr)}
    by_q = defaultdict(dict)  # q -> {ts: row}  (ts 중복 제거)
    for r in rows[1:]:
        if r[0] is None:
            continue
        by_q[str(r[0]).strip()][str(r[I["ts"]])] = r
    return I, by_q


def _pct_fill(p):
    """일관성 %에 따른 색: 높을수록 초록, 낮을수록 빨강."""
    if p >= 99:
        return PatternFill("solid", fgColor="C6E0B4")
    if p >= 95:
        return PatternFill("solid", fgColor="E2EFDA")
    if p >= 90:
        return PatternFill("solid", fgColor="FFF2CC")
    if p >= 80:
        return PatternFill("solid", fgColor="FCE4D6")
    return PatternFill("solid", fgColor="F8CBAD")


def _write_glance(wb, stages, stage_raw, stage_set, n, N,
                  resp_raw_ok, resp_set_ok, a_gen, a_retr, a_intent):
    g = wb.create_sheet("한눈에", 0)
    for col, w in zip("ABCDEF", (24, 9, 11, 11, 11, 11)):
        g.column_dimensions[col].width = w
    big = Font(bold=True, size=13)
    title = Font(bold=True, size=11)
    center = Alignment(horizontal="center")

    g["A1"] = "📊 답변 일관성 — 단계별 요약"
    g["A1"].font = big
    g["A2"] = f"비교대상: 2회+ 호출 {n}문항 / 전체 고유질문 {N}문항 (나머지 {N-n}개는 1회뿐→변동 관측불가)"
    g["A3"] = "일관성% = 같은 질문 반복호출 시 회차 간 값이 안 바뀐 문항 비율 (높을수록 결정적). set=추천 사업명 집합 기준."

    # 헤더
    r0 = 5
    heads = ["단계", "변동문항", f"일관%(/{n})\nraw", f"일관%(/{n})\nset",
             f"일관%(/{N})\nraw", f"일관%(/{N})\nset"]
    for j, h in enumerate(heads, 1):
        c = g.cell(row=r0, column=j, value=h)
        c.font = title; c.fill = PatternFill("solid", fgColor="DDEBF7")
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    rr = r0
    for st, is_set in stages:
        rr += 1
        rv = stage_raw[st]
        sv = stage_set[st] if is_set else None
        raw33 = (n - rv) / n * 100
        raw124 = (N - rv) / N * 100
        g.cell(row=rr, column=1, value=st)
        g.cell(row=rr, column=2, value=rv).alignment = center
        for col, p in ((3, raw33), (5, raw124)):
            cell = g.cell(row=rr, column=col, value=round(p, 1))
            cell.number_format = '0.0"%"'; cell.alignment = center; cell.fill = _pct_fill(p)
        if is_set:
            set33 = (n - sv) / n * 100
            set124 = (N - sv) / N * 100
            for col, p in ((4, set33), (6, set124)):
                cell = g.cell(row=rr, column=col, value=round(p, 1))
                cell.number_format = '0.0"%"'; cell.alignment = center; cell.fill = _pct_fill(p)
        else:
            for col in (4, 6):
                g.cell(row=rr, column=col, value="—").alignment = center

    # 전체 답변 일관성
    rr += 2
    g.cell(row=rr, column=1, value="■ 전체 답변 일관성 (최종응답 기준)").font = title
    for label, ok in (("문구(raw) 동일", resp_raw_ok), ("추천 사업셋 동일", resp_set_ok)):
        rr += 1
        g.cell(row=rr, column=1, value=f"  {label}")
        c33 = g.cell(row=rr, column=3, value=round(ok / n * 100, 1))
        c124 = g.cell(row=rr, column=5, value=round((ok + (N - n)) / N * 100, 1))
        for c, p in ((c33, ok / n * 100), (c124, (ok + (N - n)) / N * 100)):
            c.number_format = '0.0"%"'; c.alignment = center; c.fill = _pct_fill(p)
        g.cell(row=rr, column=2, value=f"{ok}/{n}").alignment = center

    # 원인 귀속
    rr += 2
    g.cell(row=rr, column=1, value="■ 최종응답 set 변동 원인 귀속").font = title
    for label, v, hot in (("★생성/선별 (검색동일·응답변동)", a_gen, True),
                          ("검색변동 전파", a_retr, False),
                          ("intent 변동", a_intent, False)):
        rr += 1
        g.cell(row=rr, column=1, value=f"  {label}")
        c = g.cell(row=rr, column=2, value=v); c.alignment = center
        if hot:
            c.fill = PatternFill("solid", fgColor="F8CBAD")

    g.freeze_panes = "A6"


def main():
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEF_IN
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_name(src.stem + "_consistency.xlsx")
    I, by_q = load(src)

    multi = {q: list(calls.values()) for q, calls in by_q.items() if len(calls) >= 2}
    n = len(multi)

    # 단계별 집계 + 질문별 상세
    stage_raw = {s: 0 for s, _ in STAGES}     # raw 변동 질문수
    stage_set = {s: 0 for s, _ in STAGES}     # set 변동 질문수 (set대상만)
    per_q = []  # (q, calls, {stage: (raw_distinct, set_distinct or None)})
    for q, rs in multi.items():
        detail = {}
        for s, is_set in STAGES:
            ci = I[s]
            raw_vals = {_norm(r[ci]) for r in rs}
            raw_d = len(raw_vals)
            if raw_d > 1:
                stage_raw[s] += 1
            set_d = None
            if is_set:
                sets = {svc_set(r[ci], True) for r in rs}
                set_d = len(sets)
                if set_d > 1:
                    stage_set[s] += 1
            detail[s] = (raw_d, set_d)
        per_q.append((q, len(rs), detail))

    # ── 출력 ──
    wb = Workbook()
    s = wb.active
    s.title = "Summary"
    s.column_dimensions["A"].width = 26
    s.column_dimensions["B"].width = 14
    for c in ("C", "D", "E"):
        s.column_dimensions[c].width = 16
    s.append(["■ 답변 일관성 요약", "", "", "", ""])
    s["A1"].font = Font(bold=True, size=13)
    s.append(["고유 질문수", len(by_q)])
    s.append(["실제 호출 2회+ (비교대상)", n])
    s.append(["1회만 호출 (비교불가)", len(by_q) - n])
    s.append([])
    head = ["단계", "raw 변동 질문", "raw 변동률", "set 변동 질문", "set 변동률"]
    s.append(head)
    for c in s[s.max_row]:
        c.font = BOLD; c.fill = HDR
    for st, is_set in STAGES:
        rv = stage_raw[st]
        sv = stage_set[st] if is_set else ""
        s.append([
            st, rv, f"{rv/n*100:.0f}%" if n else "—",
            sv, (f"{sv/n*100:.0f}%" if is_set and n else "—" if is_set else ""),
        ])
        rr = s.max_row
        if is_set and stage_set[st]:
            s.cell(row=rr, column=4).fill = HOT if stage_set[st] / n > 0.3 else WARN

    # ── 최종응답 set 변동 원인 귀속 ──
    resp_i = I["최종응답(response)"]
    docs_i = I["검색문서(docs)"]
    int_i = I["의도분류(intent)"]
    a_gen = a_retr = a_intent = 0
    for q, rs in multi.items():
        if len({svc_set(r[resp_i], False) for r in rs}) <= 1:
            continue
        if len({_norm(r[int_i]) for r in rs}) > 1:
            a_intent += 1
        elif len({svc_set(r[docs_i], True) for r in rs}) > 1:
            a_retr += 1
        else:
            a_gen += 1
    s.append([])
    s.append(["■ 최종응답 set 변동 원인 귀속", "", "", "", ""])
    s.cell(row=s.max_row, column=1).font = BOLD
    s.append(["  ★생성/선별 (docs 고정인데 응답변동)", a_gen])
    s.cell(row=s.max_row, column=2).fill = HOT
    s.append(["  검색변동 전파 (docs set 변동)", a_retr])
    s.append(["  intent 변동", a_intent])

    # PerQuestion 매트릭스: 각 단계 raw distinct (set대상은 set distinct를 괄호로)
    pq = wb.create_sheet("PerQuestion")
    cols = ["no", "question", "calls"] + [s for s, _ in STAGES]
    pq.append(cols)
    for j, c in enumerate(cols, 1):
        cell = pq.cell(row=1, column=j); cell.font = BOLD; cell.fill = HDR; cell.alignment = WRAP
    pq.column_dimensions["B"].width = 42
    for st_i in range(4, len(cols) + 1):
        pq.column_dimensions[get_column_letter(st_i)].width = 11
    # 변동 큰 질문 위로
    def score(item):
        _, _, d = item
        return -sum(1 for (rd, sd) in d.values() if rd > 1)
    per_q.sort(key=score)
    for qi, (q, calls, detail) in enumerate(per_q, 1):
        row = [qi, q, calls]
        for st, is_set in STAGES:
            rd, sd = detail[st]
            if is_set:
                row.append(f"{rd}({sd})" if rd > 1 or (sd and sd > 1) else "·")
            else:
                row.append(rd if rd > 1 else "·")
        pq.append(row)
        rr = pq.max_row
        pq.cell(row=rr, column=2).alignment = WRAP
        for j, (st, is_set) in enumerate(STAGES, 4):
            rd, sd = detail[st]
            cell = pq.cell(row=rr, column=j)
            if is_set and sd and sd > 1:
                cell.fill = HOT
            elif rd > 1:
                cell.fill = WARN
    pq.freeze_panes = "C2"

    # ── 한눈에 보기 시트 (단계별 일관성 %) ──
    _write_glance(wb, STAGES, stage_raw, stage_set, n, len(by_q),
                  resp_raw_ok=sum(1 for q, rs in multi.items()
                                  if len({_norm(r[I["최종응답(response)"]]) for r in rs}) == 1),
                  resp_set_ok=sum(1 for q, rs in multi.items()
                                  if len({svc_set(r[I["최종응답(response)"]], False) for r in rs}) == 1),
                  a_gen=a_gen, a_retr=a_retr, a_intent=a_intent)

    wb.save(out)

    # 콘솔
    print(f"[done] {out}")
    print(f"  고유질문 {len(by_q)} | 비교대상(2회+) {n} | 1회만 {len(by_q)-n}")
    print(f"  {'단계':<22} raw변동   set변동")
    for st, is_set in STAGES:
        rv = stage_raw[st]; sv = f"{stage_set[st]:>3}" if is_set else "  -"
        print(f"  {st:<22} {rv:>3}/{n} ({rv/n*100:>3.0f}%)  {sv}")


if __name__ == "__main__":
    main()
