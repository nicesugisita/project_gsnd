"""qa_test/260513_gsnd_total_dataset_v0.2.xlsx → /v1/chat/completions 멀티턴 실행.

규칙:
- question 컬럼 순서대로 호출.
- `[N-M]` 접두어가 붙은 행은 직전 base(no=N) 대화에 누적해서 전송 (같은 conv_id).
  접두어는 제거 후 전송.
- base 행에서 `clarified=Y`이고 봇이 시/군 되묻기면 follow_up 컬럼 값으로 후속 호출.
- 결과: qa_test/0515_answer_<timestamp>.xlsx — [question, 0515_answer] 두 컬럼.
  진행 상황 추적을 위해 부수 컬럼(no, error, elapsed_sec)도 함께 기록.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
import uuid
from pathlib import Path

import httpx
import openpyxl

ROOT = Path(__file__).resolve().parent.parent
SRC_XLSX = ROOT / "qa_test" / "260513_gsnd_total_dataset_v0.2.xlsx"
OUT_DIR = ROOT / "qa_test"
BASE_URL = "http://localhost:8000"
ENDPOINT = f"{BASE_URL}/v1/chat/completions"
TIMEOUT = 300.0
RETRY_MAX = 3
RETRY_BACKOFF_SEC = 5.0

MULTITURN_PATTERN = re.compile(r"^\[(\d+)-(\d+)\]\s*")

# 동시접속 제한은 user_id로 카운트되므로(server: _check_user_limit), 실행 내내
# 고정 user_id 하나만 쓴다. 그러면 활성 사용자 1명으로만 잡혀 50명 한도에 안 걸린다.
# 대화 격리는 conv_id가 담당하므로 user_id 공유는 안전하다. (cleanup: LIKE 'qa_v02_%')
QA_USER_ID = "qa_v02_runner"


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
            "question_raw": q_raw,
            "question_clean": q_clean,
            "base_no": base_no,
            "turn": turn,
            "clarified": clarified,
            "follow_up": follow_up,
        })
    return rows


def is_sigun_clarify(text: str) -> bool:
    t = text or ""
    return ("시/군" in t or "시군" in t) and ("거주" in t or "지역" in t or "알려" in t)


def post_chat(client: httpx.Client, messages: list[dict], user_id: str, conv_id: str) -> dict:
    payload = {"messages": messages, "user_id": user_id, "conv_id": conv_id, "stream": False}
    last_err: Exception | None = None
    for attempt in range(1, RETRY_MAX + 1):
        try:
            r = client.post(ENDPOINT, json=payload, timeout=TIMEOUT)
            if 500 <= r.status_code < 600 and attempt < RETRY_MAX:
                wait = RETRY_BACKOFF_SEC * attempt
                print(f"    [retry {attempt}/{RETRY_MAX - 1}] {r.status_code} → {wait}s 후 재시도")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError,
                httpx.ReadTimeout, httpx.WriteError) as e:
            last_err = e
            if attempt < RETRY_MAX:
                wait = RETRY_BACKOFF_SEC * attempt
                print(f"    [retry {attempt}/{RETRY_MAX - 1}] {type(e).__name__}: {e} → {wait}s 후 재시도")
                time.sleep(wait)
                continue
            raise
    raise last_err  # type: ignore[misc]


def extract_content(resp: dict) -> tuple[str, bool]:
    try:
        content = resp["choices"][0]["message"]["content"]
    except Exception:
        content = json.dumps(resp, ensure_ascii=False)[:500]
    return content, bool(resp.get("is_clarification"))


def main(limit: int | None = None, start_no: int = 1) -> None:
    if not SRC_XLSX.exists():
        print(f"[error] 입력 파일이 없습니다: {SRC_XLSX}")
        sys.exit(1)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_xlsx = OUT_DIR / f"0515_answer_{ts}.xlsx"
    out_jsonl = OUT_DIR / f"0515_answer_{ts}.jsonl"

    rows = load_rows()
    rows = [row for row in rows if row["no"] >= start_no]
    if limit:
        rows = rows[:limit]
    print(f"[run] loaded {len(rows)} rows (start_no={start_no}); xlsx={out_xlsx}")

    wb_out = openpyxl.Workbook()
    ws_out = wb_out.active
    ws_out.title = "0515_answer"
    ws_out.append(["no", "question", "0515_answer", "elapsed_sec", "error"])

    # base_no → {conv_id, user_id, messages, base_follow_up}
    sessions: dict[int, dict] = {}

    with httpx.Client() as client, out_jsonl.open("w", encoding="utf-8") as fjl:
        for i, row in enumerate(rows, 1):
            no = row["no"]
            base_no = row["base_no"]
            turn = row["turn"]
            q_send = row["question_clean"]
            q_display = row["question_raw"]
            t0 = time.monotonic()
            error = ""
            final_answer = ""

            try:
                if turn == 1 or base_no not in sessions:
                    sess = {
                        "user_id": QA_USER_ID,
                        "conv_id": str(uuid.uuid4()),
                        "messages": [],
                        "base_follow_up": row["follow_up"],
                        "base_clarified": row["clarified"],
                    }
                    sessions[base_no] = sess
                else:
                    sess = sessions[base_no]

                sess["messages"].append({"role": "user", "content": q_send})
                resp = post_chat(client, sess["messages"], sess["user_id"], sess["conv_id"])
                ans, is_clar = extract_content(resp)
                sess["messages"].append({"role": "assistant", "content": ans})
                final_answer = ans

                # base 행에서 시/군 되묻기면 자동 후속 호출 (follow_up 컬럼 값, 없으면 "창원").
                if turn == 1 and is_clar and is_sigun_clarify(ans):
                    fu = sess["base_follow_up"] or "창원"
                    sess["messages"].append({"role": "user", "content": fu})
                    resp2 = post_chat(client, sess["messages"], sess["user_id"], sess["conv_id"])
                    ans2, _ = extract_content(resp2)
                    sess["messages"].append({"role": "assistant", "content": ans2})
                    final_answer = ans2
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                final_answer = f"[ERROR] {error}"

            elapsed = round(time.monotonic() - t0, 2)
            ws_out.append([no, q_display, final_answer, elapsed, error])
            wb_out.save(out_xlsx)
            fjl.write(json.dumps({
                "no": no, "base_no": base_no, "turn": turn,
                "question": q_display, "question_sent": q_send,
                "0515_answer": final_answer, "elapsed_sec": elapsed, "error": error,
            }, ensure_ascii=False) + "\n")
            fjl.flush()

            preview = (final_answer or "")[:100].replace("\n", " ")
            tag = f" [base={base_no} turn={turn}]" if turn > 1 else ""
            print(f"[{i}/{len(rows)}] no={no}{tag} {elapsed}s | {preview}")

    wb_out.save(out_xlsx)
    print(f"[done] xlsx={out_xlsx}")
    print(f"[done] jsonl={out_jsonl}")


if __name__ == "__main__":
    # 사용법:
    #   python tests/run_gsnd_total_v02_multiturn.py            # 전체
    #   python tests/run_gsnd_total_v02_multiturn.py 5          # 처음 5개만 (스모크)
    #   python tests/run_gsnd_total_v02_multiturn.py 0 84       # no>=84 부터 전체
    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] not in ("", "0") else None
    start_no = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    main(limit=limit, start_no=start_no)
