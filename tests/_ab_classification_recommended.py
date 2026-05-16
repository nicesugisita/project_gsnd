"""classification_recommended_prompt.txt 변경 A/B 비교용 임시 스크립트.

추천 의도 케이스 4건을 동일 conv_id 분리로 호출하고 응답 + referenced_documents 저장.
변경 전(baseline)/변경 후(after) 모두 같은 셋으로 돌려 비교한다.

사용:
  python tests/_ab_classification_recommended.py baseline
  python tests/_ab_classification_recommended.py after
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent
OUT_DIR = ROOT / "llm_eval_results"
BASE_URL = "http://localhost:8000"
ENDPOINT = f"{BASE_URL}/v1/chat/completions"
TIMEOUT = 300.0

CASES: list[dict] = [
    {
        "id": "C1_changwon_elderly",
        "question": "창원 노인 복지서비스 추천해줘",
        "followup": "창원",
    },
    {
        "id": "C2_yangsan_lowincome",
        "question": "양산 저소득층 의료지원 추천해줘",
        "followup": "양산",
    },
    {
        "id": "C3_implant",
        "question": "임플란트 지원 사업 알려줘",
        "followup": "창원",
    },
    {
        "id": "C4_disabled",
        "question": "장애인 관련 지원 서비스 알려줘",
        "followup": "창원",
    },
]


def is_sigun_clarify(text: str) -> bool:
    t = text or ""
    return ("시/군" in t or "시군" in t) and ("거주" in t or "지역" in t or "알려" in t)


def post(client: httpx.Client, messages: list[dict], user_id: str, conv_id: str) -> dict:
    payload = {"messages": messages, "user_id": user_id, "conv_id": conv_id, "stream": False}
    r = client.post(ENDPOINT, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def run_case(client: httpx.Client, case: dict) -> dict:
    user_id = f"ab_{uuid.uuid4().hex[:8]}"
    conv_id = str(uuid.uuid4())
    msgs = [{"role": "user", "content": case["question"]}]
    t0 = time.monotonic()
    resp = post(client, msgs, user_id, conv_id)
    content = resp["choices"][0]["message"]["content"]
    is_clar = bool(resp.get("is_clarification"))
    reask = ""
    followup_input = ""
    final = content
    refs = resp.get("referenced_documents") or []
    if is_clar and is_sigun_clarify(content):
        reask = content
        followup_input = case["followup"]
        msgs.append({"role": "assistant", "content": content})
        msgs.append({"role": "user", "content": followup_input})
        resp2 = post(client, msgs, user_id, conv_id)
        final = resp2["choices"][0]["message"]["content"]
        refs = resp2.get("referenced_documents") or refs
    return {
        "id": case["id"],
        "question": case["question"],
        "bot_reask": reask,
        "followup_input": followup_input,
        "final_answer": final,
        "referenced_documents": refs,
        "elapsed_sec": round(time.monotonic() - t0, 2),
    }


def main(tag: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUT_DIR / f"ab_recommended_{tag}_{ts}.jsonl"
    print(f"[start] tag={tag} → {out}")
    with httpx.Client() as client, out.open("w", encoding="utf-8") as f:
        for i, case in enumerate(CASES, 1):
            try:
                r = run_case(client, case)
            except Exception as e:
                r = {"id": case["id"], "question": case["question"], "error": f"{type(e).__name__}: {e}"}
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
            preview = (r.get("final_answer") or r.get("error") or "")[:80].replace("\n", " ")
            print(f"[{i}/{len(CASES)}] {r['id']} {r.get('elapsed_sec', '?')}s | {preview}")
    print(f"[done] {out}")


if __name__ == "__main__":
    tag = sys.argv[1] if len(sys.argv) > 1 else "run"
    main(tag)
