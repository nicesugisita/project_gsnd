"""260525_경남도청_테스트질문_통합_창원.xlsx 멀티턴 실행.

규칙:
- '질문' 컬럼을 순서대로 호출(각 문항 새 conv_id, single-turn 기본).
- 봇이 시군 되물음(is_sigun_clarify)이면 '창원' 으로 후속 호출(자동).
- '후속질문(재질문)' 있으면 '|' 분할 후 첫 번째를 같은 conv에 멀티턴으로 추가 호출.
- 출력: qa_test/260525_창원_테스트결과_<ts>.xlsx — 원본 컬럼 + 답변/시군응답/후속질문선택/후속답변/경과/에러.
  진행중 유실 방지로 동일 stem .jsonl 도 행마다 flush.
"""
from __future__ import annotations
import datetime as dt, json, re, sys, time, uuid
from pathlib import Path
import httpx, openpyxl

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "qa_test" / "260525_경남도청_테스트질문_통합_창원.xlsx"
OUTDIR = ROOT / "qa_test"
ENDPOINT = "http://localhost:8000/v1/chat/completions"
TIMEOUT = 300.0
RETRY_MAX = 3
RETRY_BACKOFF = 5.0
USER_ID = "qa_changwon_runner"
SHEET = "테스트질문_창원"

def post_chat(client, messages, conv_id):
    payload = {"messages": messages, "user_id": USER_ID, "conv_id": conv_id, "stream": False}
    last = None
    for attempt in range(1, RETRY_MAX + 1):
        try:
            r = client.post(ENDPOINT, json=payload, timeout=TIMEOUT)
            if 500 <= r.status_code < 600 and attempt < RETRY_MAX:
                time.sleep(RETRY_BACKOFF * attempt); continue
            r.raise_for_status()
            return r.json()
        except (httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError,
                httpx.ReadTimeout, httpx.WriteError) as e:
            last = e
            if attempt < RETRY_MAX:
                time.sleep(RETRY_BACKOFF * attempt); continue
            raise
    raise last

def extract(resp):
    try: c = resp["choices"][0]["message"]["content"]
    except Exception: c = json.dumps(resp, ensure_ascii=False)[:500]
    return c, bool(resp.get("is_clarification"))

def is_sigun_clarify(t):
    t = t or ""
    return ("시/군" in t or "시군" in t or "어느 시" in t) and ("거주" in t or "지역" in t or "알려" in t or "어느" in t)

def pick_followup(raw):
    if not raw: return ""
    parts = [x.strip() for x in re.split(r"\s*\|\s*", str(raw)) if x.strip()]
    return parts[0] if parts else ""

def main():
    wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    ws = wb[SHEET]
    rows = list(ws.iter_rows(values_only=True))
    hdr = rows[0]
    data = [r for r in rows[1:] if r and r[4] is not None and str(r[4]).strip()]
    print(f"[run] {len(data)}문항 로드, endpoint={ENDPOINT}", flush=True)

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_xlsx = OUTDIR / f"260525_창원_테스트결과_{ts}.xlsx"
    out_jsonl = OUTDIR / f"260525_창원_테스트결과_{ts}.jsonl"

    wbo = openpyxl.Workbook(); wso = wbo.active; wso.title = "결과"
    cols = ["연번","출처","유형·카테고리","원본번호","질문","후속질문(원본)",
            "답변","시군자동응답","후속질문(선택)","후속답변","경과(초)","error"]
    wso.append(cols)

    with httpx.Client() as client, out_jsonl.open("w", encoding="utf-8") as fj:
        for i, r in enumerate(data, 1):
            yeon, src, cat, orig, q, fu_raw = r[0], r[1], r[2], r[3], str(r[4]).strip(), r[5]
            conv = str(uuid.uuid4())
            t0 = time.monotonic()
            err = ""; ans = ""; sigun_auto = ""; fu_sel = ""; fu_ans = ""
            msgs = []
            try:
                msgs.append({"role":"user","content":q})
                resp = post_chat(client, msgs, conv)
                ans, is_clar = extract(resp)
                msgs.append({"role":"assistant","content":ans})
                # 시군 되물음 → 창원 자동
                if is_clar and is_sigun_clarify(ans):
                    sigun_auto = "창원"
                    msgs.append({"role":"user","content":"창원"})
                    resp2 = post_chat(client, msgs, conv)
                    ans, _ = extract(resp2)
                    msgs.append({"role":"assistant","content":ans})
                # 후속질문
                fu_sel = pick_followup(fu_raw)
                if fu_sel:
                    msgs.append({"role":"user","content":fu_sel})
                    resp3 = post_chat(client, msgs, conv)
                    fu_ans, _ = extract(resp3)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
            el = round(time.monotonic()-t0, 1)
            row = [yeon, src, cat, orig, q, (str(fu_raw) if fu_raw else ""),
                   ans, sigun_auto, fu_sel, fu_ans, el, err]
            wso.append(row)
            fj.write(json.dumps({"연번":yeon,"질문":q,"답변":ans,"후속질문":fu_sel,
                                 "후속답변":fu_ans,"시군자동":sigun_auto,"경과":el,"error":err},
                                ensure_ascii=False)+"\n"); fj.flush()
            print(f"[{i}/{len(data)}] no={yeon} {el}s sigun={sigun_auto or '-'} fu={'Y' if fu_sel else '-'} err={err or '-'} :: {q[:28]}", flush=True)
            if i % 10 == 0:
                wbo.save(out_xlsx)
    wbo.save(out_xlsx)
    print(f"[done] 저장: {out_xlsx}", flush=True)

if __name__ == "__main__":
    main()
