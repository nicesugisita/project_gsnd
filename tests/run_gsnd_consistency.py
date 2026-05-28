"""gsnd_total_v0.2 데이터셋 질문을 N회씩 호출해 단계별 일관성(stage_trace) 비교.

`run_gsnd_total_v02_multiturn.py`(답변 1회 수집)와 달리, **각 질문/대화를 --rounds 회 반복**해
서버 response_trace.jsonl 에 쌓인 단계 산출을 회차 간 비교하는 xlsx 를 만든다.

규칙:
- 시트 `gsnd_total`, 컬럼 no / question / clarified / follow_up.
- 기본(단일턴): base 질문(turn==1)을 --rounds 회 호출(매번 새 conv_id).
- --multiturn-only: `[N-M]` 으로 묶이는 멀티턴 대화만, **대화 전체 턴을 replay** 하며 각 턴 답변을
  회차별로 비교 (그룹 키 = "[base-turn] 질문").
- 시/군 되묻기(is_clarification + 시군 패턴)면 follow_up 컬럼 값으로 후속 호출(없고 clarified=Y면 "창원").
- 각 호출 직후 trace 새 레코드(최종 답변분)를 읽어 user_question 을 원 질문으로 덮어쓴 뒤 수집.

전제: 서버가 RESPONSE_TRACE_ENABLED=True 로 떠 있어야 함.

사용:
  python tests/run_gsnd_consistency.py --no 1 --rounds 3              # 1번 단일턴 3회
  python tests/run_gsnd_consistency.py --rounds 3                     # 단일턴 전체 3회
  python tests/run_gsnd_consistency.py --multiturn-only --rounds 3    # 멀티턴 대화 전체 3회
  python tests/run_gsnd_consistency.py --multiturn-only --no 1 --rounds 3   # 멀티턴 중 base_no=1 대화만
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import time
import uuid
from collections import OrderedDict
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


def _iter_valid_rows():
    """헤더 제외, no(int)·question 있는 행만."""
    wb = openpyxl.load_workbook(SRC_XLSX, read_only=True, data_only=True)
    ws = wb["gsnd_total"]
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
        clarified = (str(r[2]).strip().upper() == "Y") if len(r) > 2 and r[2] else False
        follow_up = str(r[3]).strip() if len(r) > 3 and r[3] else ""
        yield no, q_raw, clarified, follow_up


def load_base_rows() -> list[dict]:
    """turn==1 base 질문만 (멀티턴 [N-M] 연속 행 제외)."""
    rows, skipped = [], 0
    for no, q_raw, clarified, follow_up in _iter_valid_rows():
        if MULTITURN_PATTERN.match(q_raw):
            skipped += 1
            continue
        rows.append({"no": no, "question": q_raw, "clarified": clarified, "follow_up": follow_up})
    if skipped:
        print(f"[run] 멀티턴([N-M]) 연속 행 {skipped}개 건너뜀 (단일턴 모드)")
    return rows


def load_conversations(multiturn_only: bool = True) -> list[dict]:
    """base_no 별로 턴을 묶은 대화 목록. multiturn_only면 2턴 이상만."""
    by_base: "OrderedDict[int, list[dict]]" = OrderedDict()
    for no, q_raw, clarified, follow_up in _iter_valid_rows():
        m = MULTITURN_PATTERN.match(q_raw)
        if m:
            base_no, turn = int(m.group(1)), int(m.group(2))
            q_clean = MULTITURN_PATTERN.sub("", q_raw).strip()
        else:
            base_no, turn, q_clean = no, 1, q_raw
        by_base.setdefault(base_no, []).append(
            {"no": no, "turn": turn, "question": q_clean, "clarified": clarified, "follow_up": follow_up}
        )
    convs = []
    for base_no, turns in by_base.items():
        turns.sort(key=lambda t: t["turn"])
        if multiturn_only and len(turns) <= 1:
            continue
        convs.append({"base_no": base_no, "turns": turns})
    return convs


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


def _answer_of(resp: dict) -> str:
    return resp.get("choices", [{}])[0].get("message", {}).get("content", "")


def _count_lines(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for _ in f)


def _capture_final_trace(since_line: int) -> dict | None:
    """요청 직후 trace 새 레코드의 마지막(최종 답변분). fire-and-forget 지연 대비 재시도."""
    for _ in range(6):
        recs: list[dict] = []
        if TRACE_JSONL.exists():
            with TRACE_JSONL.open("r", encoding="utf-8") as f:
                for i, line in enumerate(f):
                    if i < since_line or not line.strip():
                        continue
                    try:
                        recs.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        if recs:
            return recs[-1]
        time.sleep(0.5)
    return None


def _one_turn(client, endpoint, msgs, conv_id, turn_info) -> dict | None:
    """한 user 턴 전송(+turn1 시군 되묻기면 follow_up 후속) → 최종 답변 trace 레코드."""
    since = _count_lines(TRACE_JSONL)
    msgs.append({"role": "user", "content": turn_info["question"]})
    resp = post_chat(client, endpoint, msgs, conv_id)
    ans = _answer_of(resp)
    msgs.append({"role": "assistant", "content": ans})
    if turn_info["turn"] == 1 and bool(resp.get("is_clarification")) and is_sigun_clarify(ans):
        nxt = turn_info["follow_up"] or ("창원" if turn_info["clarified"] else "")
        if nxt:
            msgs.append({"role": "user", "content": nxt})
            resp2 = post_chat(client, endpoint, msgs, conv_id)
            msgs.append({"role": "assistant", "content": _answer_of(resp2)})
    return _capture_final_trace(since)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3, help="질문/대화당 반복 횟수")
    ap.add_argument("--no", type=int, default=None, help="특정 no(또는 멀티턴 base_no)만")
    ap.add_argument("--start-no", type=int, default=1, help="이 no 이상부터 (단일턴 resume)")
    ap.add_argument("--limit", type=int, default=None, help="처음 N개만")
    ap.add_argument("--multiturn-only", action="store_true", help="대화 단위로 전체 턴 replay (기본: 2턴 이상만)")
    ap.add_argument("--keep-single", action="store_true", help="--multiturn-only 와 함께: 1턴 대화([N-1]만 있는 단일 질문)도 포함해 replay (단일+멀티 혼합 입력용)")
    ap.add_argument("--endpoint", default="http://127.0.0.1:8000/v1/chat/completions")
    ap.add_argument("--src", default=None, help="입력 xlsx 경로(기본: 260513 gsnd_total 데이터셋). 시트 gsnd_total / no·question·clarified·follow_up 스키마.")
    ap.add_argument("--tag", default=None, help="출력 파일명에 끼울 식별 태그(예: problems) — 결과를 따로 저장.")
    args = ap.parse_args()

    global SRC_XLSX
    if args.src:
        SRC_XLSX = Path(args.src)
    if not SRC_XLSX.exists():
        print(f"[error] 입력 파일 없음: {SRC_XLSX}"); sys.exit(1)

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    suffix = "mt" if args.multiturn_only else "single"
    _tag = f"{args.tag}_" if args.tag else ""
    cap_jsonl = OUT_DIR / f"gsnd_consistency_{_tag}{suffix}_{ts}.jsonl"
    out_xlsx = OUT_DIR / f"gsnd_consistency_{_tag}{suffix}_{ts}.xlsx"
    print(f"[run] ⚠ 서버 RESPONSE_TRACE_ENABLED=True 필요. trace={TRACE_JSONL}")

    captured: list[dict] = []
    with httpx.Client() as client, cap_jsonl.open("w", encoding="utf-8") as fjl:
        if args.multiturn_only:
            convs = load_conversations(multiturn_only=not args.keep_single)
            if args.no is not None:
                convs = [c for c in convs if c["base_no"] == args.no]
            else:
                if args.start_no > 1:  # resume: 이 base_no 이상부터 (이미 끝낸 대화 건너뜀)
                    convs = [c for c in convs if c["base_no"] >= args.start_no]
                if args.limit:
                    convs = convs[: args.limit]
            if not convs:
                print("[error] 대상 멀티턴 대화 0개"); sys.exit(1)
            total_turns = sum(len(c["turns"]) for c in convs)
            print(f"[run] 멀티턴 대화 {len(convs)}개(턴 합 {total_turns}) × {args.rounds}회 | endpoint={args.endpoint}")
            for ci, c in enumerate(convs, 1):
                for rnd in range(1, args.rounds + 1):
                    conv_id = f"gcmt-{c['base_no']}-{rnd}-{uuid.uuid4().hex[:6]}"
                    msgs: list[dict] = []
                    t0 = time.monotonic()
                    n_cap = 0
                    try:
                        for tinfo in c["turns"]:
                            rec = _one_turn(client, args.endpoint, msgs, conv_id, tinfo)
                            if rec is not None:
                                rec["user_question"] = f"[{c['base_no']}-{tinfo['turn']}] {tinfo['question']}"
                                rec["_gsnd_base"] = c["base_no"]
                                rec["_turn"] = tinfo["turn"]
                                rec["_round"] = rnd
                                captured.append(rec)
                                fjl.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n"); fjl.flush()
                                n_cap += 1
                        err = "-"
                    except Exception as e:
                        err = f"{type(e).__name__}: {e}"
                    el = round(time.monotonic() - t0, 1)
                    print(f"[{ci}/{len(convs)}] base={c['base_no']} r{rnd} {el}s 턴캡처={n_cap}/{len(c['turns'])} err={err} :: {c['turns'][0]['question'][:22]}")
        else:
            rows = load_base_rows()
            if args.no is not None:
                rows = [r for r in rows if r["no"] == args.no]
            else:
                rows = [r for r in rows if r["no"] >= args.start_no]
                if args.limit:
                    rows = rows[: args.limit]
            if not rows:
                print("[error] 대상 질문 0개"); sys.exit(1)
            print(f"[run] 단일턴 질문 {len(rows)}개 × {args.rounds}회 = {len(rows) * args.rounds}회 | endpoint={args.endpoint}")
            for ri, row in enumerate(rows, 1):
                for rnd in range(1, args.rounds + 1):
                    conv_id = f"gc-{row['no']}-{rnd}-{uuid.uuid4().hex[:6]}"
                    t0 = time.monotonic()
                    err = "-"
                    try:
                        rec = _one_turn(client, args.endpoint, [], conv_id,
                                        {"turn": 1, "question": row["question"],
                                         "clarified": row["clarified"], "follow_up": row["follow_up"]})
                    except Exception as e:
                        err = f"{type(e).__name__}: {e}"; rec = None
                    el = round(time.monotonic() - t0, 1)
                    if rec is not None:
                        rec["user_question"] = row["question"]
                        rec["_gsnd_no"] = row["no"]
                        rec["_round"] = rnd
                        captured.append(rec)
                        fjl.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n"); fjl.flush()
                        n_docs = len(rec.get("top_docs") or [])
                        print(f"[{ri}/{len(rows)}] no={row['no']} r{rnd} {el}s docs={n_docs} err={err} :: {row['question'][:22]}")
                    else:
                        print(f"[{ri}/{len(rows)}] no={row['no']} r{rnd} {el}s TRACE_MISS err={err} :: {row['question'][:22]}")

    print(f"[done] 캡처 {len(captured)}건 → {cap_jsonl}")
    sys.path.insert(0, str(ROOT / "tests"))
    from export_stage_consistency import assign_rounds, build_workbook
    build_workbook(assign_rounds(captured)).save(out_xlsx)
    print(f"[done] 비교 xlsx → {out_xlsx} (diff·변동상세 시트 확인)")


if __name__ == "__main__":
    main()
