"""qa_test/260515_paraphrased_dataset.xlsx → /v1/chat/completions 멀티턴 실행.

규칙:
- 입력은 Phase 1 산출물(`base_no | turn | variant_idx | original_question | paraphrased_question
  | clarified | follow_up | original_final_answer`). 5개 변형 × 멀티턴 후속까지 모두 포함.
- 세션 키는 `(base_no, variant_idx)` — 같은 키의 행을 turn 순으로 묶어 같은 conv_id 로 호출.
- base 행(turn=1)에서 clarified=Y 이고 봇이 시/군 되묻기면 follow_up 으로 자동 후속 호출.
- 결과 xlsx + jsonl 즉시 flush (장시간 중단 안전).
- 5xx / RemoteProtocolError / ReadError / ConnectError / ReadTimeout → backoff 후 최대 3회 재시도.

베이스: tests/run_gsnd_total_v02_multiturn.py
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
import uuid
from pathlib import Path

import httpx
import openpyxl

ROOT = Path(__file__).resolve().parent.parent
SRC_XLSX = ROOT / "qa_test" / "260515_paraphrased_dataset.xlsx"
OUT_DIR = ROOT / "qa_test"

BASE_URL = "http://localhost:8000"
ENDPOINT = f"{BASE_URL}/v1/chat/completions"
TIMEOUT = 300.0
RETRY_MAX = 3
RETRY_BACKOFF_SEC = 5.0


def is_sigun_clarify(text: str) -> bool:
    t = text or ""
    return ("시/군" in t or "시군" in t) and ("거주" in t or "지역" in t or "알려" in t)


def load_rows() -> list[dict]:
    """Phase 1 산출물 xlsx 를 행 단위로 로드. variant_idx 별로 (base_no, variant_idx) 그룹화 가능."""
    wb = openpyxl.load_workbook(SRC_XLSX, read_only=True, data_only=True)
    ws = wb.active
    rows: list[dict] = []
    headers: list[str] = []
    for i, r in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            headers = [str(x or "") for x in r]
            continue
        if not r or r[0] is None:
            continue
        try:
            base_no = int(r[0])
            turn = int(r[1])
            variant_idx = int(r[2])
        except (TypeError, ValueError):
            continue
        original_q = str(r[3] or "").strip()
        paraphrased_q = str(r[4] or "").strip()
        if not paraphrased_q:
            # Phase 1 에서 5/5 미달인 행 — 건너뛰되 추적할 수 있게 빈 row 도 결과에 남김.
            rows.append({
                "base_no": base_no,
                "turn": turn,
                "variant_idx": variant_idx,
                "original_question": original_q,
                "paraphrased_question": "",
                "clarified": False,
                "follow_up": "",
                "skip": True,
            })
            continue
        clarified = (str(r[5]).strip().upper() == "Y") if r[5] else False
        follow_up = str(r[6] or "").strip()
        rows.append({
            "base_no": base_no,
            "turn": turn,
            "variant_idx": variant_idx,
            "original_question": original_q,
            "paraphrased_question": paraphrased_q,
            "clarified": clarified,
            "follow_up": follow_up,
            "skip": False,
        })
    return rows


def post_chat(client: httpx.Client, messages: list[dict], user_id: str, conv_id: str) -> dict:
    payload = {"messages": messages, "user_id": user_id, "conv_id": conv_id, "stream": False}
    last_err: Exception | None = None
    for attempt in range(1, RETRY_MAX + 1):
        try:
            r = client.post(ENDPOINT, json=payload, timeout=TIMEOUT)
            if 500 <= r.status_code < 600 and attempt < RETRY_MAX:
                wait = RETRY_BACKOFF_SEC * attempt
                print(f"    [retry {attempt}/{RETRY_MAX - 1}] {r.status_code} → {wait}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError,
                httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteError) as e:
            last_err = e
            if attempt < RETRY_MAX:
                wait = RETRY_BACKOFF_SEC * attempt
                print(f"    [retry {attempt}/{RETRY_MAX - 1}] {type(e).__name__}: {e} → {wait}s")
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


def main(limit: int | None = None, start_base_no: int = 1) -> None:
    if not SRC_XLSX.exists():
        print(f"[error] 입력 파일이 없습니다: {SRC_XLSX}")
        print("        Phase 1(build_paraphrased_dataset.py) 부터 실행하세요.")
        sys.exit(1)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_xlsx = OUT_DIR / f"0515_paraphrased_answers_{ts}.xlsx"
    out_jsonl = OUT_DIR / f"0515_paraphrased_answers_{ts}.jsonl"

    rows = load_rows()
    # (base_no, variant_idx) 세션 키별로 turn 순 정렬. limit 은 세션 단위로 적용.
    rows = [row for row in rows if row["base_no"] >= start_base_no]
    rows.sort(key=lambda x: (x["base_no"], x["variant_idx"], x["turn"]))

    if limit:
        # limit 개의 (base_no, variant_idx) 세션만 포함.
        kept_keys: list[tuple[int, int]] = []
        for row in rows:
            k = (row["base_no"], row["variant_idx"])
            if k not in kept_keys:
                kept_keys.append(k)
            if len(kept_keys) >= limit:
                break
        kept_set = set(kept_keys)
        rows = [row for row in rows if (row["base_no"], row["variant_idx"]) in kept_set]

    print(f"[run] loaded {len(rows)} rows (start_base_no={start_base_no}); xlsx={out_xlsx}")

    wb_out = openpyxl.Workbook()
    ws_out = wb_out.active
    ws_out.title = "paraphrased_answers"
    ws_out.append([
        "base_no", "turn", "variant_idx",
        "original_question", "paraphrased_question",
        "new_answer", "elapsed_sec", "error",
    ])

    # 세션 캐시: (base_no, variant_idx) → {user_id, conv_id, messages, base_follow_up, base_clarified}
    sessions: dict[tuple[int, int], dict] = {}

    with httpx.Client() as client, out_jsonl.open("w", encoding="utf-8") as fjl:
        for i, row in enumerate(rows, 1):
            base_no = row["base_no"]
            turn = row["turn"]
            variant_idx = row["variant_idx"]
            sess_key = (base_no, variant_idx)
            q = row["paraphrased_question"]
            t0 = time.monotonic()
            error = ""
            final_answer = ""

            if row.get("skip") or not q:
                error = "no paraphrased_question (Phase 1 partial/fail)"
                final_answer = "[SKIP] " + error
                ws_out.append([
                    base_no, turn, variant_idx,
                    row["original_question"], "", final_answer, 0.0, error,
                ])
                wb_out.save(out_xlsx)
                fjl.write(json.dumps({
                    "base_no": base_no, "turn": turn, "variant_idx": variant_idx,
                    "original_question": row["original_question"],
                    "paraphrased_question": "", "new_answer": final_answer,
                    "elapsed_sec": 0.0, "error": error,
                }, ensure_ascii=False) + "\n")
                fjl.flush()
                print(f"[{i}/{len(rows)}] base={base_no} v{variant_idx} t{turn} SKIP | {error}")
                continue

            try:
                if turn == 1 or sess_key not in sessions:
                    sess = {
                        "user_id": f"qa_para_{uuid.uuid4().hex[:8]}",
                        "conv_id": str(uuid.uuid4()),
                        "messages": [],
                        "base_follow_up": row["follow_up"],
                        "base_clarified": row["clarified"],
                    }
                    sessions[sess_key] = sess
                else:
                    sess = sessions[sess_key]

                sess["messages"].append({"role": "user", "content": q})
                resp = post_chat(client, sess["messages"], sess["user_id"], sess["conv_id"])
                ans, is_clar = extract_content(resp)
                sess["messages"].append({"role": "assistant", "content": ans})
                final_answer = ans

                # base 행에서만 시/군 되묻기 자동 후속 호출.
                if (turn == 1 and sess["base_clarified"] and is_clar
                        and is_sigun_clarify(ans) and sess["base_follow_up"]):
                    fu = sess["base_follow_up"]
                    sess["messages"].append({"role": "user", "content": fu})
                    resp2 = post_chat(client, sess["messages"], sess["user_id"], sess["conv_id"])
                    ans2, _ = extract_content(resp2)
                    sess["messages"].append({"role": "assistant", "content": ans2})
                    final_answer = ans2
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                final_answer = f"[ERROR] {error}"

            elapsed = round(time.monotonic() - t0, 2)
            ws_out.append([
                base_no, turn, variant_idx,
                row["original_question"], q, final_answer, elapsed, error,
            ])
            wb_out.save(out_xlsx)
            fjl.write(json.dumps({
                "base_no": base_no, "turn": turn, "variant_idx": variant_idx,
                "original_question": row["original_question"],
                "paraphrased_question": q, "new_answer": final_answer,
                "elapsed_sec": elapsed, "error": error,
            }, ensure_ascii=False) + "\n")
            fjl.flush()

            preview = (final_answer or "")[:80].replace("\n", " ")
            tag = f"base={base_no} v{variant_idx} t{turn}"
            print(f"[{i}/{len(rows)}] {tag} {elapsed}s | {preview}")

    wb_out.save(out_xlsx)
    print()
    print(f"[done] xlsx={out_xlsx}")
    print(f"[done] jsonl={out_jsonl}")


if __name__ == "__main__":
    # 사용법:
    #   python tests/run_paraphrased_questions.py            # 전체
    #   python tests/run_paraphrased_questions.py 5          # 5개 세션만 (스모크)
    #   python tests/run_paraphrased_questions.py 0 50       # base_no>=50 부터 전체
    limit = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] not in ("", "0") else None
    start_base_no = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    main(limit=limit, start_base_no=start_base_no)
