"""질문/답변 2컬럼만 가진 깔끔한 Q&A 셋을 새 xlsx로 저장.

소스 : tests/llm_eval_results/gsnd_sample_set_<timestamp>_judged.xlsx (ver1 시트)
출력 : tests/llm_eval_results/gsnd_qa_set_<timestamp>.xlsx
"""
from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "llm_eval_results"

COL_USER_INPUT = 3
COL_BOT_FINAL = 6
COL_RETRIEVED_DOC = 8
COL_VERDICT = 9


def verdict_to_label(v: str | None) -> str:
    """엄격: 정답만 맞다."""
    s = str(v or "").strip()
    if s == "정답":
        return "맞다"
    if s in ("부분정답", "오답", "평가불가"):
        return "틀리다"
    return ""


def verdict_to_label_lenient(v: str | None) -> str:
    """관대: 정답 + 부분정답 모두 맞다."""
    s = str(v or "").strip()
    if s in ("정답", "부분정답"):
        return "맞다"
    if s in ("오답", "평가불가"):
        return "틀리다"
    return ""


_BIZ_NAME_RE = re.compile(r"-?\s*사업명\s*[:：]\s*(.+?)\s*(?:\n|$)")
_PAREN_TAIL_RE = re.compile(r"\s*\([^)]+\)\s*$")
_SIGUN_PREFIX_RE = re.compile(r"^[가-힣]{2,4}(?:시|군|도)\s*")


def _normalize_biz(name: str) -> str:
    """비교용 정규화 — 공백/특수문자/시군 접두 제거."""
    s = name.strip()
    s = _SIGUN_PREFIX_RE.sub("", s)
    s = re.sub(r"[\s·.,/\-_(){}\[\]]+", "", s)
    return s.lower()


def extract_answer_biz_names(answer_text: str) -> list[str]:
    if not answer_text:
        return []
    out: list[str] = []
    for m in _BIZ_NAME_RE.finditer(answer_text):
        name = m.group(1).strip()
        if name and name not in out:
            out.append(name)
    return out


def parse_retrieved_biz_names(retrieved_text: str) -> list[str]:
    """실제 검색된 문서 셀에서 사업명만 분리 — '사업명 (시군)' → '사업명'."""
    if not retrieved_text:
        return []
    out: list[str] = []
    for raw in str(retrieved_text).splitlines():
        line = raw.strip()
        if not line:
            continue
        # 시군 접미 제거
        line = _PAREN_TAIL_RE.sub("", line).strip()
        if line and line not in out:
            out.append(line)
    return out


def check_biz_name_match(answer_names: list[str], doc_names: list[str]) -> tuple[list[tuple[str, bool]], str]:
    """답변 사업명 각각에 대해 검색된 문서 사업명과의 매칭 여부 반환.

    매칭은 정규화 후 양방향 substring 으로 판단(예: '의령군정신건강복지센터 운영' ↔ '정신건강복지센터 운영').
    """
    norm_docs = [_normalize_biz(d) for d in doc_names]
    rows: list[tuple[str, bool]] = []
    matched = 0
    for ans in answer_names:
        norm_ans = _normalize_biz(ans)
        ok = False
        if norm_ans:
            for nd in norm_docs:
                if not nd:
                    continue
                if norm_ans == nd or norm_ans in nd or nd in norm_ans:
                    ok = True
                    break
        if ok:
            matched += 1
        rows.append((ans, ok))
    if not answer_names:
        summary = "사업명 미언급"
    elif matched == len(answer_names):
        summary = f"전체 일치 ({matched}/{len(answer_names)})"
    elif matched == 0:
        summary = f"불일치 (0/{len(answer_names)})"
    else:
        summary = f"부분 일치 ({matched}/{len(answer_names)})"
    return rows, summary


def find_target() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1]).resolve()
    candidates = sorted(OUT_DIR.glob("gsnd_sample_set_*_judged.xlsx"))
    if not candidates:
        # fallback: judged 가 없으면 그냥 응답 파일 사용
        candidates = sorted(OUT_DIR.glob("gsnd_sample_set_*.xlsx"))
        candidates = [c for c in candidates if "_judged" not in c.stem]
    if not candidates:
        print("[error] 소스 xlsx 없음")
        sys.exit(1)
    return candidates[-1]


def main() -> None:
    src = find_target()
    print(f"[run] 소스: {src.name}")

    wb_in = openpyxl.load_workbook(src, data_only=True)
    ws_v1 = wb_in["ver1"]
    ws_v2 = wb_in["ver2"]

    wb_out = openpyxl.Workbook()
    ws_out = wb_out.active
    ws_out.title = "qa"
    ws_out.append([
        "질문", "답변",
        "ver1_맞다틀리다", "ver2_맞다틀리다",
        "ver1_맞다틀리다(부분정답 포함)", "ver2_맞다틀리다(부분정답 포함)",
        "답변_사업명", "문서_사업명", "사업명_일치",
    ])

    count = 0
    biz_summary = {"전체 일치": 0, "부분 일치": 0, "불일치": 0, "사업명 미언급": 0}
    for r in range(2, ws_v1.max_row + 1):
        q = ws_v1.cell(row=r, column=COL_USER_INPUT + 1).value
        a = ws_v1.cell(row=r, column=COL_BOT_FINAL + 1).value
        if not q:
            continue
        v1_raw = ws_v1.cell(row=r, column=COL_VERDICT + 1).value
        v2_raw = ws_v2.cell(row=r, column=COL_VERDICT + 1).value
        retrieved = str(ws_v1.cell(row=r, column=COL_RETRIEVED_DOC + 1).value or "")

        ans_biz = extract_answer_biz_names(str(a or ""))
        doc_biz = parse_retrieved_biz_names(retrieved)
        rows, summary = check_biz_name_match(ans_biz, doc_biz)

        # 카테고리 집계.
        if summary.startswith("전체 일치"):
            biz_summary["전체 일치"] += 1
        elif summary.startswith("부분 일치"):
            biz_summary["부분 일치"] += 1
        elif summary.startswith("불일치"):
            biz_summary["불일치"] += 1
        else:
            biz_summary["사업명 미언급"] += 1

        ans_biz_cell = "\n".join(f"{'✓' if ok else '✗'} {name}" for name, ok in rows)
        doc_biz_cell = "\n".join(doc_biz)

        ws_out.append([
            str(q).strip(),
            str(a or "").strip(),
            verdict_to_label(v1_raw),
            verdict_to_label(v2_raw),
            verdict_to_label_lenient(v1_raw),
            verdict_to_label_lenient(v2_raw),
            ans_biz_cell,
            doc_biz_cell,
            summary,
        ])
        count += 1

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUT_DIR / f"gsnd_qa_set_{ts}.xlsx"
    wb_out.save(out)
    print(f"[done] {out} ({count}행)")
    print(f"[사업명 일치 분포] 전체일치={biz_summary['전체 일치']} 부분일치={biz_summary['부분 일치']} "
          f"불일치={biz_summary['불일치']} 미언급={biz_summary['사업명 미언급']}")


if __name__ == "__main__":
    main()
