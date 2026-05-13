"""ver1의 '기대 정답 문서' 기준으로 봇 응답을 자동 채점.

전략(하이브리드)
- 1차: '기대 정답 문서'를 파싱해 본정답/[참고]/[오답유인]/메타로 분류한 뒤
       '실제 검색된 문서'와 사업명·시군 기반으로 매칭.
- 2차: 본정답이 비어있거나(◇후보·메타 노트만), 1차 결과가 애매한 경우 LLM에 위임.

입력 : tests/llm_eval_results/gsnd_sample_set_<timestamp>.xlsx
      (이미 봇 최종답변·실제 검색된 문서가 채워진 결과 파일)
기준 : tests/260512_gsnd_sample_set.xlsx 의 ver1 시트 '기대 정답 문서'
출력 : tests/llm_eval_results/gsnd_sample_set_<timestamp>_judged.xlsx
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import httpx
import openpyxl

ROOT = Path(__file__).resolve().parent
SOURCE_XLSX = ROOT / "260512_gsnd_sample_set.xlsx"
LLM_URL = "http://222.122.179.212:15006/v1/chat/completions"
LLM_TIMEOUT = 120.0
LLM_RETRY_MAX = 3
LLM_RETRY_BACKOFF = 4.0

COL_REGION = 2
COL_USER_INPUT = 3
COL_BOT_FINAL = 6
COL_EXPECTED_DOC = 7
COL_RETRIEVED_DOC = 8
COL_VERDICT = 9   # 정답 여부
COL_REASON = 10   # 사유

# 추상화된 사업명 기준 시트는 룰 substring 매칭이 약하므로 LLM에 강제 위임.
FORCE_LLM_SHEETS = {"ver2"}


# ============================================================
# Expected docs parser
# ============================================================

_TAG_REF = "[참고]"
_TAG_DECOY = "[오답유인]"
_META_PREFIX = ("※", "◇")
_DOC_PAREN = re.compile(r"\s*\(([^)]+)\)\s*$")
_NORMALIZE_SUFFIX = re.compile(r"(시|군|구|시군)\s*$")


def _normalize_sigun(s: str) -> str:
    """'진주시' / '진주군' → '진주'. 양방향 부분일치를 위해 접미사 제거."""
    s = (s or "").strip()
    return _NORMALIZE_SUFFIX.sub("", s).strip()


def _normalize_business(s: str) -> str:
    """공백/특수기호를 압축해 사업명 비교 시 노이즈 제거."""
    s = (s or "").strip()
    s = re.sub(r"[\s·.,/\-_]+", "", s)
    return s.lower()


def _split_name_sigun(line: str) -> tuple[str, str | None]:
    """'기초연금 (진주)' → ('기초연금', '진주'). 시군 없으면 (이름, None)."""
    line = line.strip()
    m = _DOC_PAREN.search(line)
    if m:
        name = line[: m.start()].strip()
        sigun = _normalize_sigun(m.group(1))
        return name, sigun
    return line, None


def parse_expected(expected_text: str) -> dict:
    """ver1 '기대 정답 문서' 셀을 분류된 구조로 파싱.

    반환:
      {
        "primary":  [(name, sigun), ...],   # 본 정답 (시군 포함)
        "reference":[(name, sigun), ...],   # [참고]
        "decoy":    [(name, sigun), ...],   # [오답유인]
        "candidates":[name, ...],            # ◇후보: 모호 질문 후보군
        "meta":     [note, ...],            # ※ 메타 노트
        "is_unevaluable": bool,             # '-' 단독 등
      }
    """
    out = {
        "primary": [],
        "reference": [],
        "decoy": [],
        "candidates": [],
        "meta": [],
        "is_unevaluable": False,
    }
    if not expected_text:
        out["is_unevaluable"] = True
        return out

    text = str(expected_text).strip()
    if text in ("-", "—"):
        out["is_unevaluable"] = True
        return out

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("◇후보:") or line.startswith("◇ 후보:"):
            tail = line.split(":", 1)[1]
            for cand in re.split(r"[/／,]", tail):
                c = cand.strip()
                # '아동급식 지원 / 아동복지시설 운영 / 가정위탁아동 (고성)' 같은
                # 줄에서 마지막 토큰의 (시군)은 후보 전체에 적용되는 게 일반적.
                c_name, _ = _split_name_sigun(c)
                if c_name:
                    out["candidates"].append(c_name)
            continue
        if line.startswith(_META_PREFIX):
            out["meta"].append(line)
            continue
        if line.startswith(_TAG_REF):
            payload = line[len(_TAG_REF):].strip()
            name, sigun = _split_name_sigun(payload)
            if name:
                out["reference"].append((name, sigun))
            continue
        if line.startswith(_TAG_DECOY):
            payload = line[len(_TAG_DECOY):].strip()
            name, sigun = _split_name_sigun(payload)
            if name:
                out["decoy"].append((name, sigun))
            continue
        # 일반 줄 — 본 정답
        name, sigun = _split_name_sigun(line)
        if name:
            out["primary"].append((name, sigun))
    return out


# ============================================================
# Retrieved docs matcher
# ============================================================

def _parse_retrieved(retrieved_text: str) -> list[tuple[str, str | None]]:
    docs: list[tuple[str, str | None]] = []
    if not retrieved_text:
        return docs
    for raw in str(retrieved_text).splitlines():
        line = raw.strip()
        if not line:
            continue
        name, sigun = _split_name_sigun(line)
        if name:
            docs.append((name, sigun))
    return docs


def _doc_matches(expected: tuple[str, str | None], actual: tuple[str, str | None]) -> tuple[bool, bool]:
    """반환 (사업명 매칭, 시군 매칭). 시군이 한쪽에 없으면 시군 매칭은 True."""
    exp_name, exp_sigun = expected
    act_name, act_sigun = actual
    en = _normalize_business(exp_name)
    an = _normalize_business(act_name)
    if not en or not an:
        name_match = False
    elif en == an or en in an or an in en:
        name_match = True
    else:
        name_match = False
    if not name_match:
        return False, False
    if not exp_sigun or not act_sigun:
        return True, True
    if exp_sigun == act_sigun or exp_sigun in act_sigun or act_sigun in exp_sigun:
        return True, True
    return True, False


def _count_hits(expected_docs: list[tuple[str, str | None]], retrieved: list[tuple[str, str | None]]) -> int:
    hits = 0
    used = set()
    for exp in expected_docs:
        for j, act in enumerate(retrieved):
            if j in used:
                continue
            name_ok, sigun_ok = _doc_matches(exp, act)
            if name_ok and sigun_ok:
                hits += 1
                used.add(j)
                break
    return hits


# ============================================================
# Rule-based verdict (1차)
# ============================================================

def rule_verdict(parsed: dict, retrieved: list[tuple[str, str | None]]) -> dict:
    """1차 룰 기반 판정.

    반환 {verdict, reason, needs_llm}
      verdict ∈ {'정답', '부분정답', '오답', '평가불가', '미정'}
      needs_llm=True 면 2차 LLM 판정 필요.
    """
    if parsed["is_unevaluable"]:
        return {"verdict": "평가불가", "reason": "기대 정답 문서가 '-'", "needs_llm": False}

    if not parsed["primary"] and parsed["candidates"]:
        # 모호 질문 — 후보군은 사업명만 있으므로 단순 매칭은 약함. LLM 위임.
        return {"verdict": "미정", "reason": "모호 질문(후보군) — LLM 판정 필요", "needs_llm": True}

    if not parsed["primary"] and not parsed["reference"] and not parsed["decoy"]:
        # 메타 노트만 있는 경우 — LLM 위임.
        return {"verdict": "미정", "reason": "메타 노트만 존재 — LLM 판정 필요", "needs_llm": True}

    primary_hits = _count_hits(parsed["primary"], retrieved)
    reference_hits = _count_hits(parsed["reference"], retrieved)
    decoy_hits = _count_hits(parsed["decoy"], retrieved)
    primary_total = len(parsed["primary"])

    pieces = []
    if primary_total:
        pieces.append(f"본정답 {primary_hits}/{primary_total}")
    if parsed["reference"]:
        pieces.append(f"참고 {reference_hits}/{len(parsed['reference'])}")
    if parsed["decoy"]:
        pieces.append(f"오답유인 {decoy_hits}/{len(parsed['decoy'])}")
    base_reason = ", ".join(pieces) if pieces else ""

    if primary_total == 0:
        # primary 없고 reference 만 있는 비정상 케이스 — LLM 위임.
        return {"verdict": "미정", "reason": f"{base_reason} — LLM 판정 필요", "needs_llm": True}

    if primary_hits == primary_total and decoy_hits == 0:
        return {"verdict": "정답", "reason": base_reason, "needs_llm": False}
    if primary_hits > 0 and decoy_hits == 0:
        return {"verdict": "부분정답", "reason": base_reason, "needs_llm": False}
    if primary_hits == 0:
        if reference_hits > 0 and decoy_hits == 0:
            return {"verdict": "부분정답", "reason": f"{base_reason} (참고만 검색)", "needs_llm": False}
        return {"verdict": "오답", "reason": base_reason, "needs_llm": False}
    # primary_hits>0 이면서 decoy 도 잡혔으면 LLM 판정
    return {"verdict": "미정", "reason": f"{base_reason} — 오답유인 동시 검색, LLM 판정 필요", "needs_llm": True}


# ============================================================
# LLM judge (2차)
# ============================================================

_JUDGE_SYSTEM_PROMPT = """너는 경상남도 복지 챗봇 RAG의 응답 채점관이다.
입력으로 받은 (사용자 질문, 기대 정답 문서 명세, 봇이 실제로 검색한 문서, 봇의 최종 답변)을 보고
정답/부분정답/오답/평가불가 중 하나로 판정하고 짧은 사유를 한 줄로 작성한다.

판정 기준
- 기대 정답 문서 명세: 본정답 줄, [참고] 참고용, [오답유인] 잡으면 안 되는 것, ◇후보 모호 질문의 후보군,
  ※ 평가 메타 노트, '-' 평가 불가.
- 정답: 본정답 문서가 검색 결과 또는 답변에 충분히 반영됨. 오답유인이 답변의 메인 결론은 아님.
- 부분정답: 본정답 일부만 반영되거나, 본정답 대신 참고 문서만 반영된 경우.
- 오답: 본정답이 전혀 반영되지 않거나, 오답유인이 답변의 핵심으로 등장.
- 평가불가: 명세 자체가 평가 불가('-')거나 메타 노트만 있어 판단 근거 부족.
- 봇이 "제공된 문서에서 확인할 수 없습니다" 류로 회피한 경우: 본정답이 명세에 있는데도 회피 → 오답.
  '-' 평가불가 케이스에서 회피 → 평가불가.

출력 형식(엄격한 JSON, 다른 텍스트 금지):
{"verdict": "정답|부분정답|오답|평가불가", "reason": "한 줄 사유"}
"""


def _build_judge_user_msg(question: str, expected: str, retrieved: str, answer: str) -> str:
    return (
        f"[사용자 질문]\n{question}\n\n"
        f"[기대 정답 문서 명세 — ver1]\n{expected}\n\n"
        f"[봇이 실제로 검색한 문서]\n{retrieved or '(없음)'}\n\n"
        f"[봇 최종 답변]\n{answer or '(없음)'}\n"
    )


def llm_judge(client: httpx.Client, question: str, expected: str, retrieved: str, answer: str) -> dict:
    payload = {
        "messages": [
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": _build_judge_user_msg(question, expected, retrieved, answer)},
        ],
        "temperature": 0,
        "max_tokens": 256,
        "stream": False,
    }
    last_err: Exception | None = None
    for attempt in range(1, LLM_RETRY_MAX + 1):
        try:
            r = client.post(LLM_URL, json=payload, timeout=LLM_TIMEOUT)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"].strip()
            # JSON 추출 (코드펜스 안에 들어 있을 수도 있음)
            m = re.search(r"\{.*\}", content, flags=re.S)
            if not m:
                raise ValueError(f"JSON 없음: {content[:200]}")
            data = json.loads(m.group(0))
            verdict = str(data.get("verdict", "")).strip()
            reason = str(data.get("reason", "")).strip()
            if verdict not in ("정답", "부분정답", "오답", "평가불가"):
                raise ValueError(f"unexpected verdict: {verdict}")
            return {"verdict": verdict, "reason": reason}
        except Exception as e:
            last_err = e
            if attempt < LLM_RETRY_MAX:
                wait = LLM_RETRY_BACKOFF * attempt
                print(f"    [llm retry {attempt}/{LLM_RETRY_MAX - 1}] {type(e).__name__}: {e} → {wait}s")
                time.sleep(wait)
                continue
            raise
    raise last_err  # type: ignore[misc]


# ============================================================
# Main
# ============================================================

def find_target_xlsx() -> Path:
    if len(sys.argv) > 1:
        return Path(sys.argv[1]).resolve()
    candidates = sorted((ROOT / "llm_eval_results").glob("gsnd_sample_set_*.xlsx"))
    candidates = [c for c in candidates if "_judged" not in c.stem]
    if not candidates:
        print("[error] tests/llm_eval_results/gsnd_sample_set_*.xlsx 가 없습니다.")
        sys.exit(1)
    return candidates[-1]


def main() -> None:
    target = find_target_xlsx()
    print(f"[run] 채점 대상: {target.name}")

    # 시트별 기대 정답 문서 로드 — ver1 sheet는 ver1 기준, ver2 sheet는 ver2 기준.
    src_wb = openpyxl.load_workbook(SOURCE_XLSX, data_only=True)
    expected_by_sheet_row: dict[str, dict[int, str]] = {}
    for sn in ("ver1", "ver2"):
        ws_src = src_wb[sn]
        d: dict[int, str] = {}
        for r in range(2, ws_src.max_row + 1):
            q = ws_src.cell(row=r, column=COL_USER_INPUT + 1).value
            if not q:
                continue
            d[r] = str(ws_src.cell(row=r, column=COL_EXPECTED_DOC + 1).value or "")
        expected_by_sheet_row[sn] = d

    # 입력 파일이 이미 _judged 면 그대로 갱신, 아니면 새 _judged 파일에 저장.
    if "_judged" in target.stem:
        out = target
    else:
        out = target.with_name(target.stem + "_judged.xlsx")

    load_src = out if out.exists() else target
    wb = openpyxl.load_workbook(load_src)

    summary_by_sheet: dict[str, dict[str, int]] = {}
    llm_calls = 0

    with httpx.Client() as client:
        for sheet_name in ("ver1", "ver2"):
            ws = wb[sheet_name]
            summary = {"정답": 0, "부분정답": 0, "오답": 0, "평가불가": 0}
            print(f"\n=== {sheet_name} 채점 시작 ===")
            for r in range(2, ws.max_row + 1):
                question = ws.cell(row=r, column=COL_USER_INPUT + 1).value
                if not question:
                    continue
                expected_text = expected_by_sheet_row[sheet_name].get(r, "")
                retrieved_text = str(ws.cell(row=r, column=COL_RETRIEVED_DOC + 1).value or "")
                answer_text = str(ws.cell(row=r, column=COL_BOT_FINAL + 1).value or "")

                parsed = parse_expected(expected_text)
                retrieved = _parse_retrieved(retrieved_text)
                rule = rule_verdict(parsed, retrieved)

                # ver2 같은 추상 사업명 시트는 룰 결과가 평가불가가 아닌 한 LLM에 위임.
                force_llm = sheet_name in FORCE_LLM_SHEETS and rule["verdict"] != "평가불가"
                if force_llm:
                    rule["needs_llm"] = True

                if rule["needs_llm"]:
                    try:
                        llm = llm_judge(client, str(question), expected_text, retrieved_text, answer_text)
                        verdict = llm["verdict"]
                        reason = f"(LLM) {llm['reason']} | rule: {rule['reason']}"
                        llm_calls += 1
                    except Exception as e:
                        verdict = "평가불가"
                        reason = f"(LLM 호출 실패) {e} | rule: {rule['reason']}"
                else:
                    verdict = rule["verdict"]
                    reason = rule["reason"]

                ws.cell(row=r, column=COL_VERDICT + 1, value=verdict)
                ws.cell(row=r, column=COL_REASON + 1, value=reason)

                summary[verdict] = summary.get(verdict, 0) + 1
                tag = " (LLM)" if rule["needs_llm"] else ""
                preview = (answer_text or "")[:50].replace("\n", " ")
                print(f"  [{sheet_name} r{r:>2}] {verdict}{tag} | {reason[:80]} | {preview}")
            summary_by_sheet[sheet_name] = summary

    wb.save(out)
    print()
    print(f"[done] {out}")
    for sn, s in summary_by_sheet.items():
        print(f"[{sn} 요약] 정답={s['정답']} 부분정답={s['부분정답']} 오답={s['오답']} 평가불가={s['평가불가']}")
    print(f"[LLM 호출 수] {llm_calls}")


if __name__ == "__main__":
    main()
