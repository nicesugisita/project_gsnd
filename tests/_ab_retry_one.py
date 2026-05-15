"""특정 케이스만 재시도."""
from __future__ import annotations
import json, sys, time, uuid
from pathlib import Path
import httpx

ENDPOINT = "http://localhost:8000/v1/chat/completions"
TIMEOUT = 300.0

CASES_BY_ID = {
    "C1_changwon_elderly": ("창원 노인 복지서비스 추천해줘", "창원"),
    "C2_yangsan_lowincome": ("양산 저소득층 의료지원 추천해줘", "양산"),
    "C3_implant": ("임플란트 지원 사업 알려줘", "창원"),
    "C4_disabled": ("장애인 관련 지원 서비스 알려줘", "창원"),
}


def is_sigun_clarify(text: str) -> bool:
    t = text or ""
    return ("시/군" in t or "시군" in t) and ("거주" in t or "지역" in t or "알려" in t)


def run(case_id: str, out_path: str) -> None:
    question, followup = CASES_BY_ID[case_id]
    user_id = f"ab_{uuid.uuid4().hex[:8]}"
    conv_id = str(uuid.uuid4())
    msgs = [{"role": "user", "content": question}]
    t0 = time.monotonic()
    with httpx.Client() as client:
        r = client.post(ENDPOINT, json={"messages": msgs, "user_id": user_id, "conv_id": conv_id, "stream": False}, timeout=TIMEOUT)
        r.raise_for_status()
        resp = r.json()
        content = resp["choices"][0]["message"]["content"]
        is_clar = bool(resp.get("is_clarification"))
        reask = ""
        followup_input = ""
        final = content
        refs = resp.get("referenced_documents") or []
        if is_clar and is_sigun_clarify(content):
            reask = content
            followup_input = followup
            msgs.append({"role": "assistant", "content": content})
            msgs.append({"role": "user", "content": followup_input})
            r2 = client.post(ENDPOINT, json={"messages": msgs, "user_id": user_id, "conv_id": conv_id, "stream": False}, timeout=TIMEOUT)
            r2.raise_for_status()
            resp2 = r2.json()
            final = resp2["choices"][0]["message"]["content"]
            refs = resp2.get("referenced_documents") or refs
    rec = {
        "id": case_id, "question": question,
        "bot_reask": reask, "followup_input": followup_input,
        "final_answer": final, "referenced_documents": refs,
        "elapsed_sec": round(time.monotonic() - t0, 2),
    }
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    preview = (final or "")[:120].replace("\n", " ")
    print(f"[ok] {case_id} {rec['elapsed_sec']}s | {preview}")


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2])
