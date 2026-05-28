"""의도분류 경량 측정 — run_unified_preprocess 만 호출(전처리만, RAG/답변 생성 없음).

queries_by_age_210.xlsx 의 query 들을 동시 호출로 전처리해 intent/search_target/reformed 를 기록.
변경 전/후 프롬프트 A/B 용. gold intent 라벨은 없으므로 분포 + 라벨 비교(diff)로 본다.

사용:
  python tests/intent_probe.py <tag> [limit] [concurrency]
  예) python tests/intent_probe.py after 210 8
"""
from __future__ import annotations
import asyncio, sys, json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import openpyxl
from app.chat._pipeline_steps import run_unified_preprocess

QFILE = ROOT / "tests" / "data" / "queries_by_age_210.xlsx"
OUTDIR = ROOT / "tests" / "llm_eval_results"


def load_queries(limit=None):
    wb = openpyxl.load_workbook(QFILE, read_only=True, data_only=True)
    ws = wb["queries"]
    qs = []
    for i, r in enumerate(ws.iter_rows(values_only=True)):
        if i == 0:
            continue
        if r and len(r) >= 4 and r[3]:
            qs.append((r[0], str(r[3]).strip()))
    return qs[:limit] if limit else qs


async def one(sem, qid, q):
    async with sem:
        try:
            pp = await run_unified_preprocess(q, [], True)
            return {"id": qid, "query": q, "intent": pp.intent,
                    "search_target": pp.search_target, "reformed": pp.reformed_query}
        except Exception as e:
            return {"id": qid, "query": q, "intent": "ERROR", "error": f"{type(e).__name__}: {e}"}


async def main(tag, limit, conc):
    qs = load_queries(limit)
    sem = asyncio.Semaphore(conc)
    results = await asyncio.gather(*[one(sem, i, q) for i, q in qs])
    OUTDIR.mkdir(parents=True, exist_ok=True)
    out = OUTDIR / f"intent_probe_{tag}.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    dist = Counter(r.get("intent", "ERR") for r in results)
    print(f"[{tag}] {len(results)}건 | 분포: {dict(dist)} | 저장: {out}")


if __name__ == "__main__":
    tag = sys.argv[1] if len(sys.argv) > 1 else "run"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] not in ("", "0") else None
    conc = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    asyncio.run(main(tag, limit, conc))
