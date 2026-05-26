"""gsnd_total_v0.2 데이터셋 질문을 N회씩 호출해 단계별 일관성(stage_trace) 비교.

`run_gsnd_total_v02_multiturn.py`(답변 1회 수집)와 달리, **각 질문을 --rounds 회 반복**해
서버 response_trace.jsonl 에 쌓인 단계 산출을 회차 간 비교하는 xlsx 를 만든다.

규칙:
- 시트 `gsnd_total`, 컬럼 no / question / clarified / follow_up.
- 각 base 질문(turn==1)을 --rounds 회 호출(매번 새 conv_id).
- 봇이 시/군 되묻기(is_clarification + 시군 패턴)면 follow_up 컬럼 값으로 후속 호출
  (follow_up 비었고 clarified=Y면 "창원").
- `[N-M]` 멀티턴 연속 행은 base 대화 맥락이 필요해 v1에선 건너뜀(로그로 표시).
- 각 호출 직후 trace 새 레코드(최종 답변분)를 읽어 user_question 을 원 질문으로 덮어쓴 뒤
  수집 → 끝나면 export_stage_consistency 로 질문별 회차 비교 xlsx 생성.

전제: 서버가 RESPONSE_TRACE_ENABLED=True 로 떠 있어야 함.

사용:
  # 1번 문항만 3회 (예시 검증)
  python tests/run_gsnd_consistency.py --no 1 --rounds 3
  # 전체 3회
  python tests/run_gsnd_consistency.py --rounds 3
  # no>=10 부터 처음 50개만, 엔드포인트 지정
  python tests/run_gsnd_consistency.py --rounds 3 --start-no 10 --limit 50 --endpoint http://127.0.0.1:8009/v1/chat/completions
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

ROOT = Path(__file__).resolve().parent.parent
SRC_XLSX = ROOT / "qa_test" / "260513_gsnd_total_dataset_v0.2.xlsx"
OUT_DIR = ROOT / "qa_test"
TRACE_JSONL = ROOT / "log" / "response_trace" / "response_trace.jsonl"
TIMEOUT = 300.0
RETRY_MAX = 3
RETRY_BACKOFF_SEC = 5.0
QA_USER_ID = "qa_consistency_runner"
MULTITURN_PATTERN = re.compile(r"^\[(\d+)-(\d+)\]\s*")


def load_base_rows() -> list[dict]:
    """turn==1 base 질문만 로드 (멀티턴 [N-M] 연속 행은 제외)."""
    wb = openpyxl.load_workbook(SRC_XLSX, read_only=True, data_only=True)
    ws = wb["gsnd_total"]
    rows: list[dict] = []
    skipped_mt = 0
    for i, r in enumerate(ws.iter_rows(values_only=True)):
        if i == 0 or not r or r[0] is None or r[1] is None:
            continue
        try:
            no = int(r[0])
        except (TypeError, ValueError):
            continue
        q_raw = str(r[1]).strip()
        if not q_raw:
            continue
        if MULTITURN_PATTERN.match(q_raw):
            skipped_mt += 1
            continue  # 멀티턴 연속 행 — v1 미지원
        clarified = (str(r[2]).strip().upper() == "Y") if len(r) > 2 and r[2] else False
        follow_up = str(r[3]).strip() if len(r) > 3 and r[3] else ""
        rows.append({"no": no, "question": q_raw, "clarified": clarified, "follow_up": follow_up})
    if skipped_mt:
        print(f"[run] 멀티턴([N-M]) 연속 행 {skipped_mt}개 건너뜀 (v1 미지원)")
    return rows


def is_sigun_clarify(text: str) -> bool:
    t = text or ""
    return ("시/군" in t or "시군" in t) and ("거주" in t or "지역" in t or "알려" in t)


def post_chat(client: httpx.Client, endpoint: str, messages: list[dict], conv_id: str) -> dict:
    payload = {"messages": messages, "user_id": QA_USER_ID, "conv_id": conv_id, "stream": False}
    last_err: Exception | None = None
    for attempt in range(1, RETRY_MAX + 1):
        try:
            r = client.post(endpoint, json=payload, timeout=TIMEOUT)
            if 500 <= r.status_code < 600 and attempt < RETRY_MAX:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError,
                httpx.ReadTimeout, httpx.WriteError) as e:
            last_err = e
            if attempt < RETRY_MAX:
                time.sleep(RETRY_BACKOFF_SEC * attempt)
                continue
            raise
    raise last_err  # type: ignore[misc]


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for _ in f)


def _read_new_records(path: Path, since_line: int) -> list[dict]:
    """since_line 이후 새로 append 된 trace 레코드들."""
    out: list[dict] = []
    if not path.exists():
        return out
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i < since_line:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _capture_final_trace(since_line: int) -> dict | None:
    """요청 직후 trace 새 레코드의 마지막(최종 답변분)을 반환. fire-and-forget 지연 대비 재시도."""
    for _ in range(6):  # 최대 ~3초 대기
        recs = _read_new_records(TRACE_JSONL, since_line)
        if recs:
            return recs[-1]
        time.sleep(0.5)
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3, help="질문당 반복 호출 횟수")
    ap.add_argument("--no", type=int, default=None, help="특정 no 질문만 실행 (예시 검증용)")
    ap.add_argument("--start-no", type=int, default=1, help="이 no 이상부터 실행 (resume)")
    ap.add_argument("--limit", type=int, default=None, help="처음 N개 질문만")
    ap.add_argument("--endpoint", default="http://127.0.0.1:8009/v1/chat/completions")
    args = ap.parse_args()

    if not SRC_XLSX.exists():
        print(f"[error] 입력 파일 없음: {SRC_XLSX}"); sys.exit(1)

    rows = load_base_rows()
    if args.no is not None:
        rows = [r for r in rows if r["no"] == args.no]
    else:
        rows = [r for r in rows if r["no"] >= args.start_no]
        if args.limit:
            rows = rows[: args.limit]
    if not rows:
        print("[error] 대상 질문 0개"); sys.exit(1)

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cap_jsonl = OUT_DIR / f"gsnd_consistency_{ts}.jsonl"
    out_xlsx = OUT_DIR / f"gsnd_consistency_{ts}.xlsx"
    print(f"[run] 질문 {len(rows)}개 × {args.rounds}회 = {len(rows) * args.rounds}회 호출 | endpoint={args.endpoint}")
    print(f"[run] ⚠ 서버 RESPONSE_TRACE_ENABLED=True 필요. trace={TRACE_JSONL}")

    captured: list[dict] = []
    with httpx.Client() as client, cap_jsonl.open("w", encoding="utf-8") as fjl:
        for ri, row in enumerate(rows, 1):
            no, q, clarified, fu = row["no"], row["question"], row["clarified"], row["follow_up"]
            for rnd in range(1, args.rounds + 1):
                conv = f"gc-{no}-{rnd}-{uuid.uuid4().hex[:6]}"
                since = _count_lines(TRACE_JSONL)
                t0 = time.monotonic()
                err = ""
                try:
                    msgs = [{"role": "user", "content": q}]
                    resp = post_chat(client, args.endpoint, msgs, conv)
                    ans = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                    is_clar = bool(resp.get("is_clarification"))
                    # 시/군 되묻기 → follow_up 후속
                    if is_clar and is_sigun_clarify(ans):
                        nxt = fu or ("창원" if clarified else "")
                        if nxt:
                            msgs += [{"role": "assistant", "content": ans}, {"role": "user", "content": nxt}]
                            post_chat(client, args.endpoint, msgs, conv)
                    rec = _capture_final_trace(since)
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    rec = None
                el = round(time.monotonic() - t0, 1)

                if rec is not None:
                    rec["user_question"] = q          # 원 질문으로 그룹 키 고정
                    rec["_gsnd_no"] = no
                    rec["_round"] = rnd
                    captured.append(rec)
                    fjl.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n"); fjl.flush()
                    n_docs = len(rec.get("top_docs") or [])
                    print(f"[{ri}/{len(rows)}] no={no} r{rnd} {el}s docs={n_docs} err={err or '-'} :: {q[:24]}")
                else:
                    print(f"[{ri}/{len(rows)}] no={no} r{rnd} {el}s TRACE_MISS err={err or '-'} :: {q[:24]}")

    print(f"[done] 캡처 {len(captured)}건 → {cap_jsonl}")
    # 비교 xlsx 생성 (export_stage_consistency 재사용)
    sys.path.insert(0, str(ROOT / "tests"))
    from export_stage_consistency import assign_rounds, build_workbook
    build_workbook(assign_rounds(captured)).save(out_xlsx)
    print(f"[done] 비교 xlsx → {out_xlsx} (diff·변동상세 시트 확인)")


if __name__ == "__main__":
    main()
