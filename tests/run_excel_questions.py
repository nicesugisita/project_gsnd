"""Excel question 컬럼을 읽어 /v1/chat/completions 에 순차 호출.

- 시/군 되묻기(is_clarification=True 이고 메시지에 '시/군' 또는 '거주' 포함)면 '창원'으로 후속 호출.
- 결과는 tests/llm_eval_results/<timestamp>.csv 로 저장.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import sys
import time
import uuid
from pathlib import Path

import httpx
import openpyxl

ROOT = Path(__file__).resolve().parent
XLSX = ROOT / "260511_gsnd_total_question_v0.1.xlsx"
OUT_DIR = ROOT / "llm_eval_results"
BASE_URL = "http://localhost:8000"
ENDPOINT = f"{BASE_URL}/v1/chat/completions"
TIMEOUT = 180.0
SIGUN_ANSWER = "창원"


def load_questions() -> list[tuple[int, str]]:
    wb = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    out: list[tuple[int, str]] = []
    for r in rows:
        if not r or r[0] is None or r[1] is None:
            continue
        try:
            no = int(r[0])
        except (TypeError, ValueError):
            continue
        q = str(r[1]).strip()
        if q:
            out.append((no, q))
    return out


def is_sigun_clarify(text: str) -> bool:
    t = text or ""
    return ("시/군" in t or "시군" in t) and ("거주" in t or "지역" in t or "알려" in t)


def post_chat(client: httpx.Client, messages: list[dict], user_id: str, conv_id: str) -> dict:
    payload = {
        "messages": messages,
        "user_id": user_id,
        "conv_id": conv_id,
        "stream": False,
    }
    r = client.post(ENDPOINT, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def extract(resp: dict) -> tuple[str, bool]:
    try:
        content = resp["choices"][0]["message"]["content"]
    except Exception:
        content = json.dumps(resp, ensure_ascii=False)[:500]
    return content, bool(resp.get("is_clarification"))


def run_one(client: httpx.Client, no: int, question: str) -> dict:
    user_id = f"qa_excel_{uuid.uuid4().hex[:8]}"
    conv_id = str(uuid.uuid4())
    msgs = [{"role": "user", "content": question}]
    t0 = time.monotonic()
    try:
        resp1 = post_chat(client, msgs, user_id, conv_id)
    except Exception as e:
        return {
            "no": no, "question": question, "first_answer": f"[ERROR] {e}",
            "clarified": False, "follow_up": "", "final_answer": "",
            "elapsed_sec": round(time.monotonic() - t0, 2), "error": str(e),
        }
    ans1, is_clar = extract(resp1)
    follow_up = ""
    final = ans1
    if is_clar and is_sigun_clarify(ans1):
        msgs.append({"role": "assistant", "content": ans1})
        msgs.append({"role": "user", "content": SIGUN_ANSWER})
        follow_up = SIGUN_ANSWER
        try:
            resp2 = post_chat(client, msgs, user_id, conv_id)
            final, _ = extract(resp2)
        except Exception as e:
            final = f"[ERROR follow-up] {e}"
    return {
        "no": no, "question": question, "first_answer": ans1,
        "clarified": is_clar, "follow_up": follow_up, "final_answer": final,
        "elapsed_sec": round(time.monotonic() - t0, 2), "error": "",
    }


def main(limit: int | None = None):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = OUT_DIR / f"results_{ts}.csv"
    jsonl_path = OUT_DIR / f"results_{ts}.jsonl"

    questions = load_questions()
    if limit:
        questions = questions[:limit]
    print(f"[run] loaded {len(questions)} questions; output={csv_path}")

    fields = ["no", "question", "first_answer", "clarified", "follow_up", "final_answer", "elapsed_sec", "error"]
    with httpx.Client() as client, csv_path.open("w", encoding="utf-8-sig", newline="") as fcsv, jsonl_path.open("w", encoding="utf-8") as fjl:
        writer = csv.DictWriter(fcsv, fieldnames=fields)
        writer.writeheader()
        for i, (no, q) in enumerate(questions, 1):
            result = run_one(client, no, q)
            writer.writerow(result)
            fcsv.flush()
            fjl.write(json.dumps(result, ensure_ascii=False) + "\n")
            fjl.flush()
            tag = " (clarified→창원)" if result["clarified"] else ""
            preview = (result["final_answer"] or "")[:120].replace("\n", " ")
            print(f"[{i}/{len(questions)}] no={no} {result['elapsed_sec']}s{tag} | {preview}")

    print(f"[done] csv={csv_path}\n[done] jsonl={jsonl_path}")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else None
    main(n)
