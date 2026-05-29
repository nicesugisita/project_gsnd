"""guide_recommend RRF 융합을 '수동 계산'으로 재현 + rerank_by_rrf() 와 대조 검증.

파이프라인(pipeline_guide_recommend.py)의 RRF 직전까지를 그대로 재현해 두 입력 리스트
(OKMS 풀 / GOV 풀)을 만든 뒤, 각 문서의 RRF 점수를

    RRF(d) = Σ_{리스트 i 에서 d 의 1-based rank}  1 / (60 + rank_i)

로 손계산해 표로 보여준다. 마지막에 코드의 rerank_by_rrf() 결과와 점수·순서가
일치하는지 자동 대조한다.

전제: 외부 Mariner·LLM 가동. RRF_FUSION_GUIDE_ENABLED=true, RELEVANCE_FILTER_ENABLED=False
      (현재 .env 기준 — 필터 ON이면 입력 풀이 SLM 필터로 줄어든다).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DEFAULT_QUESTION = "고성군에 사는 65세인데 임플란트 지원 받을 수 있어요?"

RRF_K = 60
_GR_GA_TOP_N = 15
_GR_FINAL_TOP_N_FILTER_OFF = 8
_GR_GOV_OKMS_TOP_N = 3


def _name(d: Dict[str, Any]) -> str:
    return d.get("NAME") or d.get("BUSINESS_NAME") or d.get("SERVICE_NAME") or "?"


def _cid(d: Dict[str, Any]) -> str:
    return str(d.get("CHUNK_ID") or d.get("ID") or "")


async def build_pools(message: str, pre: Dict[str, Any]):
    """pipeline_guide_recommend 의 RRF 직전 두 입력 리스트(okms_sorted, gov_sorted)를 재현."""
    from app.chat.infra.rag import (
        filter_okms_keywords, _deduplicate_documents,
        _extract_sigun_from_message, _extract_birth_year_from_message,
        _birth_year_to_lifecycle, _extract_lifecycle_from_message,
        _extract_hshd_sttn_from_message,
    )
    from app.chat.infra.rag.expansion_cap import dedupe_cap_expanded_queries
    from app.chat.infra.rag.policy_priority import (
        apply_policy_priority_to_documents, resolve_policy_boost_keywords,
    )
    from app.chat.infra.rag.pipeline_utils import (
        collect_okms_groupa_and_gov_docs, filter_gov_okms_docs_by_lifecycle,
    )
    from app.core.config import Config
    from app.mariner.queryset_okms import query_group_a_documents
    from app.mariner.queryset_gov_okms import query_gov_okms_documents
    from app.mariner.sigun_utils import normalize_sigun

    reformed = pre.get("reformed_query") or message
    tag = pre.get("policy_priority_tag")
    okms_collection = Config.RAG_OKMS_COLLECTION

    sigun_raws = _extract_sigun_from_message(message) or _extract_sigun_from_message(reformed)
    _norm = [normalize_sigun(r) for r in sigun_raws if r != "경남"]
    gr_sigun_filters = list(dict.fromkeys([s for s in _norm if s.startswith("경상남도 ")]))
    birth_year = _extract_birth_year_from_message(message)
    lifecycle = _birth_year_to_lifecycle(birth_year) if birth_year else (
        _extract_lifecycle_from_message(message) or _extract_lifecycle_from_message(reformed)
    )
    gr_hshd_sttn, gr_hshd_synonyms = _extract_hshd_sttn_from_message(f"{message} {reformed}")
    gr_year_filters = [str(date.today().year)]

    gr_expanded = dedupe_cap_expanded_queries(pre.get("expanded_queries") or [], reformed_query=reformed) or [reformed]
    precomputed_keywords = pre.get("keywords") or []
    gr_triples_list = [filter_okms_keywords(precomputed_keywords)]
    if len(gr_expanded) > 1:
        gr_triples_list.extend([[] for _ in range(len(gr_expanded) - 1)])
    gr_tri_built = [" ".join(k.strip() for k in kws if k and k.strip()) for kws in gr_triples_list]

    tags, _ = resolve_policy_boost_keywords(tag)
    boost_enabled = "low_income" not in tags

    def run_group_a(vector: str, keyword: str):
        return query_group_a_documents(
            vector, keyword, okms_collection,
            year_filters=gr_year_filters or None, sigun_filters=gr_sigun_filters,
            lifecycle_filter=lifecycle or None,
            hshd_sttn_filter=gr_hshd_sttn or None, hshd_sttn_synonyms=gr_hshd_synonyms or None,
            apply_business_anchor=False, max_results=_GR_GA_TOP_N,
        )

    def run_gov(search_str: str):
        return query_gov_okms_documents(
            search_str, collection=Config.RAG_GOV_OKMS_COLLECTION,
            lifecycle_filter=lifecycle or None, sigun_filters=gr_sigun_filters,
            hshd_sttn_filter=gr_hshd_sttn or None, hshd_sttn_synonyms=gr_hshd_synonyms or None,
            max_results=_GR_GA_TOP_N,
        )

    gr_group_a_docs, gov_okms_docs = await collect_okms_groupa_and_gov_docs(
        message=message, reformed_query=reformed, policy_priority_tag=tag,
        expanded_queries=gr_expanded, tri_built=gr_tri_built, per_query_limit=_GR_GA_TOP_N,
        run_group_a=run_group_a, run_gov=run_gov, log_prefix="rrf_manual",
        log_skip_empty_triple=True, policy_search_boost_enabled=boost_enabled,
    )

    gr_group_a_top = sorted(
        _deduplicate_documents(gr_group_a_docs),
        key=lambda x: float(x.get("WEIGHT", 0) or 0), reverse=True,
    )[:_GR_GA_TOP_N]

    gov_pool = filter_gov_okms_docs_by_lifecycle(_deduplicate_documents(gov_okms_docs), lifecycle)
    gov_candidates = sorted(gov_pool, key=lambda x: float(x.get("WEIGHT", 0) or 0), reverse=True)
    gov_cand_ids = {d.get("CHUNK_ID") for d in gov_candidates if d.get("CHUNK_ID")}

    # D-1: 정책 부스트 재정렬(필터 OFF → 그대로 survivors)
    merged = list(gr_group_a_top) + list(gov_candidates)
    merged = apply_policy_priority_to_documents(tag, merged, log_prefix="rrf_manual", apply_enabled=boost_enabled)

    gov_surv = [d for d in merged if d.get("CHUNK_ID") in gov_cand_ids]
    okms_surv = [d for d in merged if d.get("CHUNK_ID") not in gov_cand_ids]
    okms_sorted = sorted(okms_surv, key=lambda x: float(x.get("WEIGHT", 0) or 0), reverse=True)
    gov_sorted = sorted(gov_surv, key=lambda x: float(x.get("WEIGHT", 0) or 0), reverse=True)
    return okms_sorted, gov_sorted, tag


def print_list(title: str, docs: List[Dict[str, Any]]) -> None:
    print(f"\n── {title} (rank: 1/(60+rank)) ──")
    for r, d in enumerate(docs, 1):
        print(f"  rank {r:>2}  1/(60+{r})={1/(RRF_K+r):.6f}  {_name(d)}  [CID={_cid(d)}, W={d.get('WEIGHT')}]")


def main_calc(okms: List[Dict[str, Any]], gov: List[Dict[str, Any]]):
    """수동 RRF 계산 — 키(CID)별 두 리스트 rank 합산."""
    okms_rank = {_cid(d): r for r, d in enumerate(okms, 1)}
    gov_rank = {_cid(d): r for r, d in enumerate(gov, 1)}
    rep: Dict[str, Dict[str, Any]] = {}
    for d in okms:
        rep.setdefault(_cid(d), d)
    for d in gov:
        rep.setdefault(_cid(d), d)

    rows = []
    for cid, d in rep.items():
        ro = okms_rank.get(cid)
        rg = gov_rank.get(cid)
        co = 1 / (RRF_K + ro) if ro else 0.0
        cg = 1 / (RRF_K + rg) if rg else 0.0
        rows.append((cid, d, ro, rg, co, cg, co + cg))
    rows.sort(key=lambda t: t[6], reverse=True)
    return rows


async def main_async(message: str, cap: int) -> None:
    from app.mariner.jvm_manager import init_jvm
    from app.chat.preprocessing import unified_preprocess
    from app.chat.infra.rag.rrf_reranker import rerank_by_rrf, SCORE_RRF

    print(f"[질문] {message}")
    init_jvm()
    pre = await unified_preprocess(message)
    if pre.get("intent") != "guide_recommend":
        print(f"⚠ intent={pre.get('intent')} — 이 스크립트는 guide_recommend RRF 전용입니다.")
    okms, gov, tag = await build_pools(message, pre)

    print(f"\npolicy_priority_tag={tag} | OKMS 풀={len(okms)}건, GOV 풀={len(gov)}건 | RRF_K={RRF_K} | cap={cap}")
    print_list("입력 리스트 A: OKMS (WEIGHT 내림차순)", okms)
    print_list("입력 리스트 B: GOV (WEIGHT 내림차순)", gov)

    rows = main_calc(okms, gov)

    print("\n" + "=" * 110)
    print("수동 RRF 계산 결과 (RRF = 1/(60+okms_rank) + 1/(60+gov_rank), 내림차순)")
    print("=" * 110)
    print(f"{'순위':>3} {'RRF점수':>10} {'A(OKMS)':>8} {'B(GOV)':>8}  문서  [CID]")
    for i, (cid, d, ro, rg, co, cg, tot) in enumerate(rows, 1):
        mark = "  ← cap" if i == cap else ""
        a = f"#{ro}" if ro else "-"
        b = f"#{rg}" if rg else "-"
        print(f"{i:>3} {tot:>10.6f} {a:>8} {b:>8}  {_name(d)[:46]}  [{cid}]{mark}")

    # ── 검증: 코드 rerank_by_rrf 와 대조 ──
    fused = rerank_by_rrf(okms, gov)
    ok = True
    for i, (cid, d, *_rest, tot) in enumerate(rows):
        code_doc = fused[i]
        code_cid = _cid(code_doc)
        code_score = float(code_doc.get(SCORE_RRF, 0) or 0)
        if code_cid != cid or abs(code_score - tot) > 1e-9:
            ok = False
            print(f"  [불일치] 수동 #{i+1} {cid}({tot:.6f}) vs 코드 {code_cid}({code_score:.6f})")
    print("\n[검증] 수동 계산 == rerank_by_rrf() : ", "✅ 완전 일치" if ok else "❌ 불일치 (위 참조)")

    print(f"\n[최종] guide_recommend 가 취하는 상위 {cap}건 (cap=_GR_FINAL_TOP_N+_GR_GOV_OKMS_TOP_N):")
    for i, (cid, d, ro, rg, co, cg, tot) in enumerate(rows[:cap], 1):
        src = "GOV" if rg else "OKMS"
        print(f"  {i:>2}. [{src}] {_name(d)}  (RRF={tot:.6f}, CID={cid})")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", "--question", dest="question", default=DEFAULT_QUESTION)
    ap.add_argument("--cap", type=int, default=_GR_FINAL_TOP_N_FILTER_OFF + _GR_GOV_OKMS_TOP_N)
    args = ap.parse_args()
    asyncio.run(main_async(args.question, args.cap))


if __name__ == "__main__":
    main()
