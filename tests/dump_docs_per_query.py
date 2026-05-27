"""컬렉션 × 검색식별 문서 검색 결과 덤프.

서버 파이프라인은 여러 컬렉션(OKMS GSND_BIZ_DATASET_V4 / GOV_OKMS_V1, 또는 search 의도면
WELFARE_CENTER / OUR_REGION_TEL)에 각기 다른 검색식(WHERE 트리)으로 질의한 뒤, 결과를 합쳐
(dedup·정렬·리랭킹·관련성필터) 버린다. 그래서 "어느 컬렉션의, 어느 검색식이, 어떤 문서를
끌어왔는지"가 합산 단계에서 사라진다. 이 스크립트는 그 합산 직전을 재현해
**[컬렉션] → [검색식(WHERE)] → [검색된 문서]** 순으로 분리 출력한다.

충실성:
- 쿼리 생성은 실제 `unified_preprocess`(LLM). 서버가 만드는 expanded_queries/keywords/
  policy_priority_tag/intent 와 동일.
- 검색은 서버와 같은 queryset 함수를 같은 필터·파라미터로 호출.
- 검색식(WHERE)은 서버가 Mariner 에 실제로 넘기는 WhereSet 트리를 stage_trace.render_whereset
  로 렌더한 것 (record_search_query 가 setWhere 직전에 기록). RESPONSE_TRACE_ENABLED 필요.
- 합산/리랭킹/관련성필터는 적용하지 않고 검색식별 원본 결과를 그대로 보여준다.

전제: 외부 Mariner(.env MARINER_IP)·LLM(.env LLM_API_URL) 가동 + RESPONSE_TRACE_ENABLED=True.

사용:
    python tests/dump_docs_per_query.py
    python tests/dump_docs_per_query.py --q "고성군에 사는 65세인데 임플란트 지원 받을 수 있어요?"
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from itertools import zip_longest
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

DEFAULT_QUESTION = "고성군에 사는 65세인데 임플란트 지원 받을 수 있어요?"


def _fmt_docs(docs: List[Dict[str, Any]], limit: int = 30) -> str:
    if not docs:
        return "      (검색 결과 0건)"
    lines: List[str] = []
    for i, d in enumerate(docs[:limit], 1):
        name = (
            d.get("NAME") or d.get("BUSINESS_NAME") or d.get("SERVICE_NAME")
            or d.get("FACILITY_NAME") or "?"
        )
        lines.append(
            f"      {i:>2}. {name}"
            f"  | W={d.get('WEIGHT', '?')}"
            f"  SIGUN={d.get('SIGUN', '-')}"
            f"  YEAR={d.get('YEAR', '-')}"
            f"  LIFE_CYCLE={d.get('LIFE_CYCLE', '-')}"
            f"  CID={d.get('CHUNK_ID', '-')}"
        )
    if len(docs) > limit:
        lines.append(f"      … 외 {len(docs) - limit}건")
    return "\n".join(lines)


def _collection_banner(title: str) -> None:
    print("\n" + "#" * 100)
    print(f"#  컬렉션: {title}")
    print("#" * 100)


def _print_expr(idx: int, leg: str, input_q: str, where: str, docs: List[Dict[str, Any]]) -> None:
    print(f"\n  ┌─ 검색식 #{idx}" + (f"  [{leg}]" if leg else ""))
    print(f"  │  검색어 입력 : {input_q!r}")
    print(f"  │  WHERE(검색식): {where or '(미기록 — RESPONSE_TRACE_ENABLED 확인)'}")
    print(f"  └─ 검색결과 {len(docs)}건:")
    print(_fmt_docs(docs))


def _search_with_trace(call: Callable[[], Any]) -> Tuple[Any, List[Dict[str, Any]]]:
    """queryset 호출 전후로 stage_trace 를 reset/snapshot 해 이 호출의 검색식만 수확."""
    from app.chat.infra.rag import stage_trace
    stage_trace.reset()
    result = call()
    snap = stage_trace.snapshot()
    return result, list(snap.get("search_queries") or [])


def _where_for(entries: List[Dict[str, Any]], label_suffix: str | None = None) -> str:
    """라벨 접미(#0/#1 등)로 검색식 1건 선택. 없으면 첫 항목."""
    if not entries:
        return ""
    if label_suffix is not None:
        for e in entries:
            if str(e.get("label", "")).endswith(label_suffix):
                return e.get("where", "")
    return entries[0].get("where", "")


async def dump_guide_recommend(message: str, pre: Dict[str, Any]) -> None:
    from app.chat.infra.rag import (
        filter_okms_keywords,
        _extract_sigun_from_message,
        _extract_birth_year_from_message,
        _birth_year_to_lifecycle,
        _extract_lifecycle_from_message,
        _extract_hshd_sttn_from_message,
    )
    from app.chat.infra.rag.expansion_cap import dedupe_cap_expanded_queries
    from app.chat.infra.rag.pipeline_utils import _okms_dual_query_for_search
    from app.chat.infra.rag.policy_priority import (
        policy_extra_okms_searches, resolve_policy_boost_keywords,
    )
    from app.core.config import Config
    from app.mariner.queryset_okms import query_group_a_documents
    from app.mariner.queryset_gov_okms import query_gov_okms_documents
    from app.mariner.sigun_utils import normalize_sigun

    reformed = pre.get("reformed_query") or message
    tag = pre.get("policy_priority_tag")
    okms_collection = Config.RAG_OKMS_COLLECTION
    gov_collection = Config.RAG_GOV_OKMS_COLLECTION

    # --- 필터 추출 (pipeline_guide_recommend.py Step0 와 동일 순서) ---
    sigun_raws = _extract_sigun_from_message(message) or _extract_sigun_from_message(reformed)
    _norm = [normalize_sigun(r) for r in sigun_raws if r != "경남"]
    gr_sigun_filters = list(dict.fromkeys([s for s in _norm if s.startswith("경상남도 ")]))
    birth_year = _extract_birth_year_from_message(message)
    if birth_year:
        lifecycle = _birth_year_to_lifecycle(birth_year)
    else:
        lifecycle = _extract_lifecycle_from_message(message) or _extract_lifecycle_from_message(reformed)
    gr_hshd_sttn, gr_hshd_synonyms = _extract_hshd_sttn_from_message(f"{message} {reformed}")
    gr_year_filters = [str(date.today().year)]

    # --- 쿼리 생성 (서버와 동일: precomputed 사용) ---
    gr_expanded = dedupe_cap_expanded_queries(
        pre.get("expanded_queries") or [], reformed_query=reformed
    ) or [reformed]
    precomputed_keywords = pre.get("keywords") or []
    gr_triples_list = [filter_okms_keywords(precomputed_keywords)]
    if len(gr_expanded) > 1:
        gr_triples_list.extend([[] for _ in range(len(gr_expanded) - 1)])
    gr_tri_built = [" ".join(k.strip() for k in kws if k and k.strip()) for kws in gr_triples_list]

    tags, _ = resolve_policy_boost_keywords(tag)
    boost_enabled = "low_income" not in tags

    print("\n" + "=" * 100)
    print("[guide_recommend] 추출된 필터 / 생성된 쿼리")
    print("=" * 100)
    print(f"  reformed_query     : {reformed}")
    print(f"  policy_priority_tag: {tag}  (boost_enabled={boost_enabled})")
    print(f"  sigun_filters      : {gr_sigun_filters}")
    print(f"  lifecycle          : {lifecycle or '(미적용)'}  (birth_year={birth_year})")
    print(f"  house_situation    : {gr_hshd_sttn or '(미적용)'}  synonyms={gr_hshd_synonyms}")
    print(f"  year_filters       : {gr_year_filters}")
    print(f"  확장쿼리(vector)   : {gr_expanded}")
    print(f"  트리플쿼리(keyword): {gr_tri_built}")

    _GR_GA_MAX_RESULTS = 15

    def run_group_a(vector: str, keyword: str):
        return query_group_a_documents(
            vector, keyword, okms_collection,
            year_filters=gr_year_filters or None, sigun_filters=gr_sigun_filters,
            lifecycle_filter=lifecycle or None,
            hshd_sttn_filter=gr_hshd_sttn or None, hshd_sttn_synonyms=gr_hshd_synonyms or None,
            apply_business_anchor=False, max_results=_GR_GA_MAX_RESULTS,
        )

    def run_gov(search_str: str):
        return query_gov_okms_documents(
            search_str, collection=gov_collection,
            lifecycle_filter=lifecycle or None, sigun_filters=gr_sigun_filters,
            hshd_sttn_filter=gr_hshd_sttn or None, hshd_sttn_synonyms=gr_hshd_synonyms or None,
            max_results=_GR_GA_MAX_RESULTS,
        )

    ga_pairs: List[Tuple[str, str]] = [
        _okms_dual_query_for_search(
            eq, sq if sq else "",
            policy_priority_tag=tag, policy_search_boost_enabled=boost_enabled,
        )
        for eq, sq in zip_longest(gr_expanded, gr_tri_built, fillvalue="")
    ]
    policy_extra = policy_extra_okms_searches(tag, reformed) if boost_enabled else []

    # ===== 컬렉션 1: OKMS (듀얼 — vector leg / keyword leg 각각 별도 검색식) =====
    _collection_banner(f"{okms_collection}  (OKMS Group A — 듀얼: keyword leg=OKMS#0, vector leg=OKMS#1)")
    expr_no = 0
    all_pairs = [("본검색", p) for p in ga_pairs] + [("정책보강", p) for p in policy_extra]
    for kind, (vec, kw) in all_pairs:
        (kw_docs, vec_docs), entries = _search_with_trace(lambda v=vec, k=kw: run_group_a(v, k))
        expr_no += 1
        _print_expr(expr_no, f"{kind} · vector leg", vec, _where_for(entries, "#1"), vec_docs)
        if kw.strip():
            expr_no += 1
            _print_expr(expr_no, f"{kind} · keyword leg", kw, _where_for(entries, "#0"), kw_docs)
        else:
            print(f"\n  · {kind} keyword leg: 키워드 없음 → 서버에서도 건너뜀")

    # ===== 컬렉션 2: GOV_OKMS (KO 키워드 + MI 벡터 하이브리드, 검색식 1개/쿼리) =====
    _gov_seen: set = set()
    gov_strings: List[str] = []
    for vec, kw in [*ga_pairs, *policy_extra]:
        for s in (vec, kw):
            s = (s or "").strip()
            if s and s not in _gov_seen:
                _gov_seen.add(s)
                gov_strings.append(s)
    if not gov_strings and reformed.strip():
        gov_strings = [reformed.strip()]

    _collection_banner(f"{gov_collection}  (GOV_OKMS — KO 키워드 + MI 벡터 하이브리드 4필드 OR)")
    for i, s in enumerate(gov_strings, 1):
        docs, entries = _search_with_trace(lambda q=s: run_gov(q))
        _print_expr(i, "GOV_OKMS", s, _where_for(entries), docs)


async def dump_search(message: str, pre: Dict[str, Any]) -> None:
    from app.chat.infra.rag.expansion_cap import dedupe_cap_expanded_queries
    from app.chat.infra.rag.pipeline_utils import build_welfare_search_queries
    from app.chat.sigun import extract_eupmyeondong_from_message
    from app.chat.infra.rag import _extract_sigun_from_message
    from app.core.config import Config
    from app.mariner.queryset_welfare import query_welfare_center_documents
    from app.mariner.queryset_welfare_tel import query_welfare_tel_documents
    from app.mariner.sigun_utils import normalize_sigun

    reformed = pre.get("reformed_query") or message
    tag = pre.get("policy_priority_tag")
    sigun_raws = _extract_sigun_from_message(message) or _extract_sigun_from_message(reformed)
    _norm = [normalize_sigun(r) for r in sigun_raws if r != "경남"]
    sigun_filters = list(dict.fromkeys([s for s in _norm if s.startswith("경상남도 ")]))
    emd = extract_eupmyeondong_from_message(message)
    emd_filters = [emd] if emd else []

    expanded = dedupe_cap_expanded_queries(pre.get("expanded_queries") or [], reformed_query=reformed) or [reformed]
    keyword_str = " ".join(pre.get("keywords") or [])
    search_queries = [keyword_str] if keyword_str.strip() else []
    all_queries, policy_qs = build_welfare_search_queries(
        expanded_queries=expanded, search_queries=search_queries,
        policy_priority_tag=tag, policy_search_boost_enabled=True,
    )

    print("\n" + "=" * 100)
    print("[search] 추출된 필터 / 생성된 쿼리")
    print("=" * 100)
    print(f"  reformed_query: {reformed}")
    print(f"  search_target : {pre.get('search_target')}")
    print(f"  sigun_filters : {sigun_filters}   eupmyeondong: {emd_filters}")
    print(f"  검색 쿼리 목록 : {all_queries}")
    print(f"  정책 보강 쿼리 : {policy_qs}")

    _collection_banner(f"{Config.RAG_WELFARE_CENTER_COLLECTION}  (WELFARE_CENTER)")
    for i, q in enumerate(all_queries, 1):
        try:
            (docs), entries = _search_with_trace(
                lambda qq=q: query_welfare_center_documents(qq, sigun_filters=sigun_filters)
            )
        except Exception as e:
            docs, entries = [], []
            print(f"  (CENTER 검색 실패: {e})")
        _print_expr(i, "WELFARE_CENTER", q, _where_for(entries), docs)

    _collection_banner(f"{Config.RAG_WELFARE_TEL_COLLECTION}  (OUR_REGION_TEL)")
    for i, q in enumerate(all_queries, 1):
        try:
            docs, entries = _search_with_trace(
                lambda qq=q: query_welfare_tel_documents(
                    qq, sigun_filters=sigun_filters, eupmyeondong_filters=emd_filters, max_results=-1)
            )
        except Exception as e:
            docs, entries = [], []
            print(f"  (TEL 검색 실패: {e})")
        _print_expr(i, "OUR_REGION_TEL", q, _where_for(entries), docs)


async def main_async(message: str) -> None:
    from app.mariner.jvm_manager import init_jvm
    from app.chat.preprocessing import unified_preprocess
    from app.core.config import Config

    print(f"[질문] {message}")
    if not getattr(Config, "RESPONSE_TRACE_ENABLED", False):
        print("⚠ RESPONSE_TRACE_ENABLED=False — 검색식(WHERE) 렌더가 비활성입니다. 문서 결과만 출력됩니다.")
    print("[1/2] JVM 초기화 + Mariner 접속 …")
    init_jvm()
    print("[2/2] unified_preprocess(LLM) 로 실제 쿼리 생성 …")
    pre = await unified_preprocess(message)
    intent = pre.get("intent")
    print(f"  → intent={intent}")

    if intent == "search":
        await dump_search(message, pre)
    else:
        if intent != "guide_recommend":
            print(f"  ⚠ intent={intent} 이지만 OKMS(guide_recommend) 흐름으로 덤프합니다.")
        await dump_guide_recommend(message, pre)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", "--question", dest="question", default=DEFAULT_QUESTION)
    args = ap.parse_args()
    asyncio.run(main_async(args.question))


if __name__ == "__main__":
    main()
