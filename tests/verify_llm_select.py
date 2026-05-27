"""GUIDE_LLM_RELEVANCE_SELECT 모드 검증 — 플래그 ON 으로 guide_recommend 파이프라인 end-to-end.

확인 항목:
- RRF 상위 11건이 최종응답 LLM 에 전달되는가 (referenced_documents 수 ≤ 11)
- 선별 프롬프트가 쓰이고 가변개수(D-1.8)가 생략되는가 (로그 마커)
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

Q = "고성군에 사는 65세인데 임플란트 지원 받을 수 있어요?"


async def run() -> None:
    from app.core.config import Config
    Config.GUIDE_LLM_RELEVANCE_SELECT_ENABLED = True  # 런타임 토글

    from app.mariner.jvm_manager import init_jvm
    from app.chat.preprocessing import unified_preprocess
    from app.chat.infra.rag.pipeline_guide_recommend import process_rag_guide_recommend

    init_jvm()
    pre = await unified_preprocess(Q)
    print(f"\n[pre] intent={pre.get('intent')} tag={pre.get('policy_priority_tag')} "
          f"expanded={len(pre.get('expanded_queries') or [])} keywords={len(pre.get('keywords') or [])}")

    resp, refs = await process_rag_guide_recommend(
        message=Q, reformed_query=pre.get("reformed_query") or Q,
        temperature=0, max_tokens=2048, stream=False,
        frequency_penalty=0, repetition_penalty=1.0, top_p=1.0, top_k=1, seed=42, tools=[],
        messages=[{"role": "user", "content": Q}],
        precomputed_expanded_queries=pre.get("expanded_queries"),
        precomputed_keywords=pre.get("keywords"),
        precomputed_policy_priority_tag=pre.get("policy_priority_tag"),
    )
    text = resp if isinstance(resp, str) else str(resp)
    n_cards = text.count("[서비스 ")
    print("\n" + "=" * 90)
    print(f"[검증] LLM 에 전달된 문서 수(referenced_documents) = {len(refs)}  (cap={Config.GUIDE_LLM_SELECT_MAX_DOCS})")
    print(f"[검증] 11건 이하 = {'OK' if len(refs) <= Config.GUIDE_LLM_SELECT_MAX_DOCS else 'FAIL'}")
    print(f"[검증] 응답 카드 수([서비스 N]) = {n_cards}  (LLM 이 관련만 선별한 결과)")
    print("=" * 90)
    print("[전달된 문서]")
    for i, d in enumerate(refs, 1):
        print(f"  {i:>2}. {d.get('name') or d.get('NAME') or d.get('business_name') or '?'}")
    print("\n[응답 일부]\n" + text[:900])


if __name__ == "__main__":
    asyncio.run(run())
