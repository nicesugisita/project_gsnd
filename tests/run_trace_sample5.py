"""qa_test/260513_gsnd_total_dataset_v0.3.xlsx 의 N개 행으로 응답 트레이스 검증.

전제:
- 서버가 RESPONSE_TRACE_ENABLED=True 가 적용된 상태로 http://localhost:8000 에서 가동 중이어야 한다.
- 토글 변경 후에는 서버 재시작이 필요하다 (Config 가 시동 시 .env 로드).

사용:
    python tests/run_trace_sample5.py          # 기본 5건
    python tests/run_trace_sample5.py 10       # 첫 10건
    python tests/run_trace_sample5.py all      # 전체 (160건)
    python tests/run_trace_sample5.py 50 --start 10   # row 11~60 (1-based, --start 10이면 10건 skip)

동작:
1) 데이터셋에서 question 추출 (start 만큼 skip 후 N건).
2) /v1/chat/completions 호출. 봇이 시군 되묻기를 하면 데이터셋 follow_up 컬럼 값으로 후속 턴.
3) 호출 종료 후 log/response_trace/response_trace.jsonl 의 새 행 개수 검증.
"""

from __future__ import annotations

import argparse
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
QA_DIR = ROOT.parent / "qa_test"
XLSX_IN = QA_DIR / "260513_gsnd_total_dataset_v0.3.xlsx"
TRACE_JSONL = ROOT.parent / "log" / "response_trace" / "response_trace.jsonl"
TRACE_XLSX = ROOT.parent / "log" / "response_trace" / "response_trace.xlsx"

BASE_URL = "http://localhost:8000"
ENDPOINT = f"{BASE_URL}/v1/chat/completions"
TIMEOUT = 300.0
FALLBACK_SIGUN = "창원"


def _read_rows(path: Path, start: int, limit: int | None) -> list[dict]:
    """start 만큼 skip 후 limit 개 (limit=None 이면 전체) 읽기."""
    wb = openpyxl.load_workbook(path, read_only=True)
    ws = wb["gsnd_total"]
    rows = list(ws.iter_rows(values_only=True))
    header = list(rows[0])
    idx = {name: i for i, name in enumerate(header)}
    out: list[dict] = []
    skipped = 0
    for r in rows[1:]:
        if not r or not r[idx["question"]]:
            continue
        if skipped < start:
            skipped += 1
            continue
        out.append({
            "no": r[idx["no"]],
            "question": str(r[idx["question"]]).strip(),
            "clarified": str(r[idx["clarified"]] or "").strip().upper(),
            "follow_up": str(r[idx["follow_up"]] or "").strip(),
        })
        if limit is not None and len(out) >= limit:
            break
    return out


def _is_sigun_clarify(text: str) -> bool:
    t = text or ""
    return ("시/군" in t or "시군" in t) and ("거주" in t or "지역" in t or "알려" in t)


def _post_chat(client: httpx.Client, messages: list[dict], user_id: str, conv_id: str) -> dict:
    payload = {
        "messages": messages,
        "user_id": user_id,
        "conv_id": conv_id,
        "stream": False,
    }
    r = client.post(ENDPOINT, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _extract_content(resp: dict) -> tuple[str, bool]:
    try:
        content = resp["choices"][0]["message"]["content"]
    except Exception:
        content = json.dumps(resp, ensure_ascii=False)[:500]
    return content, bool(resp.get("is_clarification"))


def _count_jsonl_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def _read_last_n_jsonl(path: Path, n: int) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as f:
        lines = [l for l in f.readlines() if l.strip()]
    return [json.loads(l) for l in lines[-n:]]


def run_one(client: httpx.Client, row: dict) -> dict:
    user_id = f"trace_sample_{uuid.uuid4().hex[:8]}"
    conv_id = str(uuid.uuid4())
    msgs: list[dict] = [{"role": "user", "content": row["question"]}]

    t0 = time.monotonic()
    resp1 = _post_chat(client, msgs, user_id, conv_id)
    ans1, is_clar = _extract_content(resp1)
    bot_reask = ""
    followup_input = ""
    final_answer = ans1

    if is_clar and _is_sigun_clarify(ans1):
        bot_reask = ans1
        followup_input = row["follow_up"] or FALLBACK_SIGUN
        msgs.append({"role": "assistant", "content": ans1})
        msgs.append({"role": "user", "content": followup_input})
        resp2 = _post_chat(client, msgs, user_id, conv_id)
        final_answer, _ = _extract_content(resp2)

    return {
        "no": row["no"],
        "question": row["question"],
        "bot_reask": bot_reask,
        "followup_input": followup_input,
        "final_answer_preview": (final_answer or "")[:120].replace("\n", " "),
        "final_answer_len": len(final_answer or ""),
        "elapsed_sec": round(time.monotonic() - t0, 2),
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="응답 트레이스 검증 — 데이터셋 N개 행 호출")
    p.add_argument("limit", nargs="?", default="5",
                   help="처리 건수 (숫자 또는 'all'). 기본 5")
    p.add_argument("--start", type=int, default=0,
                   help="앞에서 skip 할 행 수 (0-based offset). 기본 0")
    p.add_argument("--base-url", default=BASE_URL,
                   help=f"챗 API base URL. 기본 {BASE_URL}")
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    limit: int | None = None if str(args.limit).lower() == "all" else int(args.limit)
    base_url = args.base_url
    endpoint = f"{base_url}/v1/chat/completions"

    if not XLSX_IN.exists():
        print(f"[error] 데이터셋 없음: {XLSX_IN}")
        sys.exit(1)

    # 서버 health
    try:
        r = httpx.get(f"{base_url}/health", timeout=5)
        r.raise_for_status()
    except Exception as e:
        print(f"[error] 서버({base_url}) 헬스체크 실패: {e}")
        print("        — uvicorn 으로 서버를 띄운 뒤 다시 실행하세요.")
        sys.exit(2)

    # endpoint 를 run_one 에서 참조하므로 모듈 전역에 주입
    global ENDPOINT
    ENDPOINT = endpoint

    before = _count_jsonl_lines(TRACE_JSONL)
    print(f"[info] base_url={base_url} | start={args.start} | limit={limit if limit else 'all'}")
    print(f"[info] 사전 JSONL 행 수: {before}")

    rows = _read_rows(XLSX_IN, args.start, limit)
    print(f"[run] 처리할 질문 {len(rows)}건")

    t_start = time.monotonic()
    results: list[dict] = []
    with httpx.Client() as client:
        for i, row in enumerate(rows, 1):
            try:
                res = run_one(client, row)
            except Exception as e:
                res = {
                    "no": row["no"], "question": row["question"],
                    "error": f"{type(e).__name__}: {e}",
                }
            results.append(res)
            elapsed_total = time.monotonic() - t_start
            avg = elapsed_total / i
            eta_sec = avg * (len(rows) - i)
            print(f"[{i}/{len(rows)}] no={res.get('no')} elapsed={res.get('elapsed_sec', '-')}s "
                  f"len={res.get('final_answer_len', '-')} avg={avg:.1f}s eta={eta_sec/60:.1f}m | "
                  f"{res.get('final_answer_preview', res.get('error', ''))}")

    # 트레이스 검증
    after = _count_jsonl_lines(TRACE_JSONL)
    delta = after - before
    print()
    print(f"[trace] JSONL 행 수: {before} → {after} (+{delta})")
    print(f"[trace] xlsx 존재: {TRACE_XLSX.exists()}")

    if delta == 0:
        print("[warn] 새로 추가된 trace 행이 0건 — 서버가 새 코드로 재시작되지 않았거나")
        print("        RESPONSE_TRACE_ENABLED 가 False 일 수 있습니다.")
    else:
        recent = _read_last_n_jsonl(TRACE_JSONL, delta)
        print(f"[trace] 마지막 {len(recent)} 행 검증:")
        for rec in recent:
            ok = bool(rec.get("response_text"))
            uq = (rec.get("user_question") or "")[:40].replace("\n", " ")
            rt_len = len(rec.get("response_text") or "")
            print(f"  - trace_id={rec.get('trace_id')} intent={rec.get('intent')} "
                  f"is_stream={rec.get('is_stream')} response_len={rt_len} ok={ok} | q={uq}")

    print()
    print(f"[done] JSONL: {TRACE_JSONL}")
    print(f"[done] xlsx : {TRACE_XLSX}")


if __name__ == "__main__":
    main()
