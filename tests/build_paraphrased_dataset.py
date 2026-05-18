"""qa_test/260513_gsnd_total_dataset_v0.2.xlsx → paraphrase 5개씩 생성.

규칙:
- 모든 행(멀티턴 `[N-M]` 후속 포함) 대상.
- 외부 32B LLM(`http://222.122.179.212:15006/v1/chat/completions`) 호출로 각 질문당 5개 변형 생성.
- 결과: `qa_test/260515_paraphrased_dataset.xlsx`
  컬럼: base_no | turn | variant_idx | original_question | paraphrased_question
        | clarified | follow_up | original_final_answer
- 한 행 처리 후 즉시 xlsx flush — 장시간 실행 중 중단 안전.
- 5개 미만 응답이면 1회 재시도. 그래도 부족하면 받은 개수만 저장 + error 컬럼 표기.

사용:
    python tests/build_paraphrased_dataset.py            # 전체
    python tests/build_paraphrased_dataset.py 1          # 처음 1행 (스모크)
    python tests/build_paraphrased_dataset.py 0 50       # no>=50 부터 전체
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import httpx
import openpyxl

ROOT = Path(__file__).resolve().parent.parent
SRC_XLSX = ROOT / "qa_test" / "260513_gsnd_total_dataset_v0.2.xlsx"
OUT_XLSX = ROOT / "qa_test" / "260515_paraphrased_dataset.xlsx"

LLM_URL = "http://222.122.179.212:15006/v1/chat/completions"
LLM_TIMEOUT = 120.0
LLM_RETRY_MAX = 3
LLM_RETRY_BACKOFF = 4.0

VARIANTS_PER_Q = 5
TEMPERATURE = 0.7
MAX_TOKENS = 512

MULTITURN_PATTERN = re.compile(r"^\[(\d+)-(\d+)\]\s*")

SYSTEM_PROMPT = """너는 한국어 복지 챗봇 평가용 패러프레이즈 생성기다.
주어진 사용자 질문을 의미가 동일한 5개의 변형으로 다시 표현하라.
반드시 유지: 나이/세대(예: 만 65세, 청년, 임신부), 지원 분야(주거, 의료, 양육 등),
             지역명, 신청·자격·금액 등 구체 키워드.
바꿔도 됨: 어순, 어조(반말/존댓말), 동의어, 의문형/명사형, 호칭.
출력은 JSON 배열 5개 문자열만. 다른 텍스트 금지.
예시 출력: ["변형1", "변형2", "변형3", "변형4", "변형5"]
"""

HEADERS = [
    "base_no", "turn", "variant_idx",
    "original_question", "paraphrased_question",
    "clarified", "follow_up", "original_final_answer",
    "error",
]


def load_rows() -> list[dict]:
    wb = openpyxl.load_workbook(SRC_XLSX, read_only=True, data_only=True)
    ws = wb["gsnd_total"]
    rows: list[dict] = []
    for i, r in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue
        if not r or r[0] is None or r[1] is None:
            continue
        try:
            no = int(r[0])
        except (TypeError, ValueError):
            continue
        q_raw = str(r[1]).strip()
        if not q_raw:
            continue
        clarified = (str(r[2]).strip().upper() == "Y") if r[2] else False
        follow_up = str(r[3]).strip() if r[3] else ""
        # final_answer 는 col 4 또는 5 (헤더 위치에 따라 다를 수 있음 — 둘 다 시도)
        final_answer = ""
        if len(r) > 4 and r[4] is not None:
            final_answer = str(r[4]).strip()
        m = MULTITURN_PATTERN.match(q_raw)
        if m:
            base_no = int(m.group(1))
            turn = int(m.group(2))
            q_clean = MULTITURN_PATTERN.sub("", q_raw).strip()
        else:
            base_no = no
            turn = 1
            q_clean = q_raw
        rows.append({
            "no": no,
            "base_no": base_no,
            "turn": turn,
            "question_raw": q_raw,
            "question_clean": q_clean,
            "clarified": clarified,
            "follow_up": follow_up,
            "final_answer": final_answer,
        })
    return rows


def _extract_json_array(content: str) -> list[str] | None:
    """LLM 응답에서 JSON 배열을 추출한다. 코드펜스/잡텍스트 무시."""
    if not content:
        return None
    # 가장 바깥 [...] 매칭
    m = re.search(r"\[[\s\S]*\]", content)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    out = []
    for x in data:
        if isinstance(x, str):
            s = x.strip()
            if s:
                out.append(s)
    return out or None


def call_paraphrase(client: httpx.Client, question: str) -> tuple[list[str], str]:
    """5개 변형을 반환. 실패 시 (받은 만큼, error 메시지)."""
    payload = {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": question},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }
    last_err: Exception | None = None
    for attempt in range(1, LLM_RETRY_MAX + 1):
        try:
            r = client.post(LLM_URL, json=payload, timeout=LLM_TIMEOUT)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            variants = _extract_json_array(content) or []
            if len(variants) >= VARIANTS_PER_Q:
                return variants[:VARIANTS_PER_Q], ""
            # 5개 미만 — 응답을 살려 두고, 1회 추가 재시도 (RETRY_MAX 안에서).
            if attempt < LLM_RETRY_MAX:
                wait = LLM_RETRY_BACKOFF * attempt
                print(f"    [retry {attempt}/{LLM_RETRY_MAX - 1}] only {len(variants)} variants → {wait}s")
                time.sleep(wait)
                continue
            return variants, f"only {len(variants)} variants returned"
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError,
                httpx.ReadTimeout, httpx.WriteError, httpx.HTTPStatusError) as e:
            last_err = e
            if attempt < LLM_RETRY_MAX:
                wait = LLM_RETRY_BACKOFF * attempt
                print(f"    [retry {attempt}/{LLM_RETRY_MAX - 1}] {type(e).__name__}: {e} → {wait}s")
                time.sleep(wait)
                continue
            return [], f"{type(e).__name__}: {e}"
        except Exception as e:
            last_err = e
            return [], f"{type(e).__name__}: {e}"
    return [], f"{type(last_err).__name__}: {last_err}" if last_err else "unknown error"


def _open_or_create_output() -> tuple[openpyxl.Workbook, "openpyxl.worksheet.worksheet.Worksheet", set[tuple[int, int]]]:
    """기존 출력 파일이 있으면 (base_no, turn) 진행 키를 수집해서 반환 (재개용)."""
    if OUT_XLSX.exists():
        wb = openpyxl.load_workbook(OUT_XLSX)
        ws = wb.active
        done: set[tuple[int, int]] = set()
        for r in range(2, ws.max_row + 1):
            b = ws.cell(row=r, column=1).value
            t = ws.cell(row=r, column=2).value
            if b is not None and t is not None:
                try:
                    done.add((int(b), int(t)))
                except (TypeError, ValueError):
                    pass
        return wb, ws, done
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "paraphrased"
    ws.append(HEADERS)
    return wb, ws, set()


def main(limit: int | None = None, start_no: int = 1) -> None:
    if not SRC_XLSX.exists():
        print(f"[error] 입력 파일이 없습니다: {SRC_XLSX}")
        sys.exit(1)
    OUT_XLSX.parent.mkdir(parents=True, exist_ok=True)

    rows = load_rows()
    rows = [row for row in rows if row["no"] >= start_no]
    if limit:
        rows = rows[:limit]
    print(f"[run] loaded {len(rows)} rows (start_no={start_no}); out={OUT_XLSX}")

    wb_out, ws_out, done = _open_or_create_output()
    if done:
        print(f"[run] 기존 결과에서 (base_no, turn) {len(done)}개 이미 처리됨 — skip.")

    with httpx.Client() as client:
        total_calls = 0
        total_ok = 0
        total_partial = 0
        total_fail = 0
        for i, row in enumerate(rows, 1):
            key = (row["base_no"], row["turn"])
            if key in done:
                continue
            q = row["question_clean"]
            t0 = time.monotonic()
            variants, err = call_paraphrase(client, q)
            elapsed = round(time.monotonic() - t0, 2)
            total_calls += 1

            if len(variants) == VARIANTS_PER_Q and not err:
                total_ok += 1
            elif variants:
                total_partial += 1
            else:
                total_fail += 1

            # 받은 만큼 다 기록. 부족하면 빈 칸도 행으로 남겨 추적성 보장.
            for idx in range(VARIANTS_PER_Q):
                paraphrased = variants[idx] if idx < len(variants) else ""
                row_err = err if idx >= len(variants) else ""
                ws_out.append([
                    row["base_no"], row["turn"], idx + 1,
                    row["question_raw"], paraphrased,
                    "Y" if row["clarified"] else "",
                    row["follow_up"],
                    row["final_answer"],
                    row_err,
                ])
            wb_out.save(OUT_XLSX)

            preview = (variants[0] if variants else "")[:60].replace("\n", " ")
            tag = f"base={row['base_no']} turn={row['turn']}"
            status = f"{len(variants)}/{VARIANTS_PER_Q}"
            err_tag = f" err={err}" if err else ""
            print(f"[{i}/{len(rows)}] {tag} {elapsed}s {status}{err_tag} | {preview}")

    print()
    print(f"[done] calls={total_calls} ok={total_ok} partial={total_partial} fail={total_fail}")
    print(f"[done] xlsx={OUT_XLSX}")


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] not in ("", "0") else None
    start_no = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    main(limit=limit, start_no=start_no)
