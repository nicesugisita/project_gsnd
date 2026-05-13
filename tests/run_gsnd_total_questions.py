"""260511_gsnd_total_question_v0.1.xlsx Sheet1 의 140개 질문을 /v1/chat/completions에 던지고 결과 저장.

- 시군 되묻기(is_clarification=True 이고 메시지에 '시/군' 또는 '거주' 포함)면 '창원' 으로 후속 호출.
- 결과는 tests/llm_eval_results/gsnd_total_<timestamp>.xlsx 와 .jsonl 두 형식으로 저장.
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

ROOT = Path(__file__).resolve().parent
XLSX = ROOT / "llm_eval_results" / "260511_gsnd_total_question_v0.1.xlsx"
OUT_DIR = ROOT / "llm_eval_results"
BASE_URL = "http://localhost:8000"
ENDPOINT = f"{BASE_URL}/v1/chat/completions"
TIMEOUT = 300.0
SIGUN_ANSWER = "창원"
RETRY_MAX = 3
RETRY_BACKOFF_SEC = 5.0


def load_questions() -> list[tuple[int, str]]:
    wb = openpyxl.load_workbook(XLSX, read_only=True, data_only=True)
    ws = wb["Sheet1"]
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
    last_err: Exception | None = None
    for attempt in range(1, RETRY_MAX + 1):
        try:
            r = client.post(ENDPOINT, json=payload, timeout=TIMEOUT)
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


_DOC_NAME_PATTERN = re.compile(r"^(?:20\d{2}_)?(?P<sigun>[^_]+)_(?P<title>.+?)(?:\.[A-Za-z0-9]+)?$")


def _format_doc_name(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return raw
    m = _DOC_NAME_PATTERN.match(raw)
    if m:
        return f"{m.group('title')} ({m.group('sigun')})"
    if "." in raw:
        raw = raw.rsplit(".", 1)[0]
    return raw


def extract_referenced(resp: dict) -> str:
    refs = resp.get("referenced_documents") or []
    if not refs:
        return ""
    lines: list[str] = []
    seen: set[str] = set()
    for d in refs:
        name = str(d.get("name") or "").strip()
        doc_id = str(d.get("id") or "").strip()
        label = _format_doc_name(name) if name else (f"id={doc_id}" if doc_id else "")
        if label and label not in seen:
            seen.add(label)
            lines.append(label)
    return "\n".join(lines)


def run_one(client: httpx.Client, no: int, question: str) -> dict:
    user_id = f"qa_total_{uuid.uuid4().hex[:8]}"
    conv_id = str(uuid.uuid4())
    msgs: list[dict] = [{"role": "user", "content": question}]

    t0 = time.monotonic()
    try:
        resp1 = post_chat(client, msgs, user_id, conv_id)
    except Exception as e:
        return {
            "no": no, "question": question,
            "first_answer": f"[ERROR] {e}",
            "clarified": False, "follow_up": "", "final_answer": "",
            "referenced_docs": "",
            "elapsed_sec": round(time.monotonic() - t0, 2),
            "error": str(e),
        }
    ans1, is_clar = extract_content(resp1)
    refs1 = extract_referenced(resp1)
    follow_up = ""
    final = ans1
    referenced = refs1
    if is_clar and is_sigun_clarify(ans1):
        msgs.append({"role": "assistant", "content": ans1})
        msgs.append({"role": "user", "content": SIGUN_ANSWER})
        follow_up = SIGUN_ANSWER
        try:
            resp2 = post_chat(client, msgs, user_id, conv_id)
            final, _ = extract_content(resp2)
            referenced = extract_referenced(resp2) or refs1
        except Exception as e:
            final = f"[ERROR follow-up] {e}"
    return {
        "no": no, "question": question,
        "first_answer": ans1,
        "clarified": is_clar, "follow_up": follow_up, "final_answer": final,
        "referenced_docs": referenced,
        "elapsed_sec": round(time.monotonic() - t0, 2),
        "error": "",
    }


def main(limit: int | None = None, start_no: int = 1, append_to: Path | None = None) -> None:
    """limit: 처리할 최대 행 수 (no 기준 X, 슬라이스 길이). start_no: 처리 시작 no (1-based).
    append_to: 지정하면 기존 xlsx/jsonl에 이어서 저장.
    """
    if not XLSX.exists():
        print(f"[error] 입력 파일이 없습니다: {XLSX}")
        sys.exit(1)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if append_to is not None:
        xlsx_path = append_to
        jsonl_path = append_to.with_suffix(".jsonl")
        if not xlsx_path.exists():
            print(f"[error] append 대상 xlsx 없음: {xlsx_path}")
            sys.exit(1)
        print(f"[run] APPEND 모드 — {xlsx_path}")
    else:
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        xlsx_path = OUT_DIR / f"gsnd_total_{ts}.xlsx"
        jsonl_path = OUT_DIR / f"gsnd_total_{ts}.jsonl"

    questions = load_questions()
    questions = [(no, q) for no, q in questions if no >= start_no]
    if limit:
        questions = questions[:limit]
    print(f"[run] loaded {len(questions)} questions (start_no={start_no}); xlsx={xlsx_path} jsonl={jsonl_path}")

    if append_to is not None:
        wb_out = openpyxl.load_workbook(xlsx_path)
        ws_out = wb_out["gsnd_total"] if "gsnd_total" in wb_out.sheetnames else wb_out.active
        jsonl_mode = "a"
    else:
        wb_out = openpyxl.Workbook()
        ws_out = wb_out.active
        ws_out.title = "gsnd_total"
        ws_out.append([
            "no", "question",
            "clarified", "follow_up", "first_answer", "final_answer",
            "referenced_docs", "elapsed_sec", "error",
        ])
        jsonl_mode = "w"

    with httpx.Client() as client, jsonl_path.open(jsonl_mode, encoding="utf-8") as fjl:
        for i, (no, q) in enumerate(questions, 1):
            r = run_one(client, no, q)
            ws_out.append([
                r["no"], r["question"],
                "Y" if r["clarified"] else "",
                r["follow_up"], r["first_answer"], r["final_answer"],
                r["referenced_docs"], r["elapsed_sec"], r["error"],
            ])
            wb_out.save(xlsx_path)  # 매 행마다 저장 (장시간 실행 대비)
            fjl.write(json.dumps(r, ensure_ascii=False) + "\n")
            fjl.flush()
            tag = " (clarified→창원)" if r["clarified"] else ""
            preview = (r["final_answer"] or r["first_answer"] or "")[:100].replace("\n", " ")
            print(f"[{i}/{len(questions)}] no={r['no']} {r['elapsed_sec']}s{tag} | {preview}")

    wb_out.save(xlsx_path)
    print(f"[done] xlsx={xlsx_path}")
    print(f"[done] jsonl={jsonl_path}")


if __name__ == "__main__":
    # 사용법:
    #   python run_gsnd_total_questions.py                       # 전체 1~140 새 파일
    #   python run_gsnd_total_questions.py 20                    # 처음 20개 새 파일
    #   python run_gsnd_total_questions.py 0 21 <기존_xlsx>       # 기존 파일에 no>=21부터 append (limit=0 ⇒ 전체)
    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] not in ("", "0") else None
    start_no = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    append_to = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else None
    main(limit=limit, start_no=start_no, append_to=append_to)
