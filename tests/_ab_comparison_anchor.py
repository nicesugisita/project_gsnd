"""비교 검색 사업명 앵커 비활성화 회귀 실측.

함양/산청 난임 시술비 비교 질의로 함양군 난임 사업이 retrieve 되는지 확인.
"""
from __future__ import annotations
import datetime as dt, json, sys, time, uuid
from pathlib import Path
import httpx

ENDPOINT = "http://localhost:8000/v1/chat/completions"
TIMEOUT = 300.0
OUT_DIR = Path(__file__).resolve().parent / "llm_eval_results"

CASES = [
    {
        "id": "CMP1_hyam_sancheong_fertility",
        "question": "함양군이랑 산청군에서 난임 시술비 지원 조건이 같아요?",
        "followup": None,
        "expect_contains": ["함양", "산청"],
    },
]


def run(case, tag):
    user_id = f"ab_{uuid.uuid4().hex[:8]}"
    conv_id = str(uuid.uuid4())
    msgs = [{"role": "user", "content": case["question"]}]
    t0 = time.monotonic()
    with httpx.Client() as client:
        r = client.post(ENDPOINT, json={"messages": msgs, "user_id": user_id, "conv_id": conv_id, "stream": False}, timeout=TIMEOUT)
        r.raise_for_status()
        resp = r.json()
        final = resp["choices"][0]["message"]["content"]
        refs = resp.get("referenced_documents") or []
    elapsed = round(time.monotonic() - t0, 2)
    ref_names = [str(d.get("name") or d.get("id") or "?").strip() for d in refs]
    contains = {k: any(k in n for n in ref_names) for k in case["expect_contains"]}
    return {
        "id": case["id"], "tag": tag, "question": case["question"],
        "final_answer": final, "referenced_documents": refs,
        "ref_names": ref_names, "contains": contains,
        "elapsed_sec": elapsed,
    }


def main(tag):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUT_DIR / f"ab_comparison_anchor_{tag}_{ts}.jsonl"
    print(f"[start] tag={tag} → {out}")
    with out.open("w", encoding="utf-8") as f:
        for i, case in enumerate(CASES, 1):
            try:
                r = run(case, tag)
            except Exception as e:
                r = {"id": case["id"], "tag": tag, "error": f"{type(e).__name__}: {e}"}
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
            if "error" in r:
                print(f"[{i}/{len(CASES)}] {r['id']} ERROR: {r['error']}")
            else:
                print(f"[{i}/{len(CASES)}] {r['id']} {r['elapsed_sec']}s")
                print(f"   contains: {r['contains']}")
                for n in r['ref_names']:
                    mark = "  ★함양" if "함양" in n else ("  ●산청" if "산청" in n else "")
                    print(f"     - {n}{mark}")
    print(f"[done] {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "after")
