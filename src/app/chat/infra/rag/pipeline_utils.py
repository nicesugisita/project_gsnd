"""RAG 파이프라인 공통 유틸리티 함수"""

import asyncio
import logging
import re
from contextlib import contextmanager
from itertools import zip_longest
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from app.core.constants import ROLE_USER
from app.core.config import Config
from app.chat.infra.rag.policy_priority import (
    augment_okms_dual_query,
    policy_extra_okms_searches,
    policy_supplement_welfare_queries,
    resolve_policy_boost_keywords,
)

from .document import _get_document_snippet, _get_document_name

logger = logging.getLogger(__name__)


FALLBACK_MAX_EXPANDED_DEFAULT = 2
FALLBACK_MAX_EXPANDED_ELDERLY = 1
FALLBACK_REJUDGE_MIN_DOC_GAIN = 1
FALLBACK_REJUDGE_MIN_TOP_WEIGHT_GAIN = 0.03
SUFFICIENCY_JUDGMENT_TIMEOUT_SEC = 4.0
SUFFICIENCY_JUDGMENT_MAX_DOCS = 8

# ---- 적합성 판정 휴리스틱 단축 ----
# 검색 결과가 명확히 충분한 케이스(상위 문서 BUSINESS_NAME에 사용자 핵심어가 그대로 들어있는 경우)는
# LLM 판정을 생략한다. 보수적으로 적용해 false-positive 위험 최소화.
SUFFICIENCY_SHORTCUT_MIN_DOCS = 3
SUFFICIENCY_SHORTCUT_MIN_ANCHOR_LEN = 3
# 시군명/일반어는 anchor 후보에서 배제해 false-positive 차단
_SUFFICIENCY_SHORTCUT_ANCHOR_STOPWORDS = {
    "지원", "정보", "안내", "신청", "방법", "대상", "조건", "사업",
    "제도", "서비스", "복지", "문의", "내용", "절차", "기준",
}
_SUFFICIENCY_NUMERIC_RE = re.compile(r"^\d{1,4}(?:년|년도)?$")


def _tokenize_for_shortcut(text: str) -> List[str]:
    tokens: List[str] = []
    for raw in str(text or "").split():
        tok = re.sub(r"[^0-9A-Za-z가-힣]", "", raw).strip()
        if len(tok) < SUFFICIENCY_SHORTCUT_MIN_ANCHOR_LEN:
            continue
        if tok in _SUFFICIENCY_SHORTCUT_ANCHOR_STOPWORDS:
            continue
        if _SUFFICIENCY_NUMERIC_RE.fullmatch(tok):
            continue
        tokens.append(tok)
    return tokens


def _doc_business_name(doc: Dict[str, Any]) -> str:
    for key in ("BUSINESS_NAME", "BUSINESS_NAME_KO", "NAME", "ORG_NM"):
        value = str(doc.get(key, "") or "").strip()
        if value:
            return value
    return ""


def shortcut_sufficiency_by_anchor(
    *,
    user_question: str,
    docs: List[Dict[str, Any]],
    sigun_filters: Optional[List[str]] = None,
    log_prefix: str = "RAG",
) -> Optional[Dict[str, Any]]:
    """검색 결과가 명확히 충분하면 LLM 판정 없이 sufficient=True 반환.

    조건(모두 충족 시 단축):
    - docs 개수 >= SUFFICIENCY_SHORTCUT_MIN_DOCS
    - 사용자 질의에서 추출한 anchor 토큰(>=3자, 시군명/일반어/연도 제외)이 존재
    - 상위 1건 문서의 BUSINESS_NAME 류 필드에 anchor 포함

    조건 미충족 시 None → 호출측은 LLM 판정으로 폴백.
    """
    if not docs or len(docs) < SUFFICIENCY_SHORTCUT_MIN_DOCS:
        return None

    candidate_tokens = _tokenize_for_shortcut(user_question)
    if not candidate_tokens:
        return None

    # 시군명 토큰은 anchor 후보에서 제외 (예: "창원", "진주")
    banned: set[str] = set()
    for s in sigun_filters or []:
        s = str(s or "").strip()
        if not s:
            continue
        banned.add(s)
        banned.add(s.replace("경상남도", "").strip())
        banned.update(_tokenize_for_shortcut(s))

    anchors = [t for t in candidate_tokens if t not in banned]
    if not anchors:
        return None

    top_name = _doc_business_name(docs[0])
    if not top_name:
        return None

    matched_anchor = next((t for t in anchors if t in top_name), None)
    if not matched_anchor:
        return None

    reason = f"shortcut_anchor_top1:{matched_anchor}"
    logger.info(
        "[%s] sufficiency shortcut: %s | top_name=%s | docs=%d",
        log_prefix, reason, top_name[:60], len(docs),
    )
    return {"sufficient": True, "reason": reason}


@contextmanager
def log_step_banner(logger_obj: logging.Logger, label: str):
    """스텝 시작/끝 배너 로그."""
    logger_obj.debug("-----------[%s 시작]-----------", label)
    try:
        yield
    finally:
        logger_obj.debug("-----------[%s 끝]-----------", label)


def _build_documents_text(doc_list: List[Dict[str, Any]]) -> str:
    """문서 목록을 문자열로 포맷팅"""
    documents = []
    for doc in doc_list:
        chunk_path = _get_document_snippet(doc)
        chunk_id = doc.get("CHUNK_ID", "")
        name = _get_document_name(doc)
        if chunk_path:
            documents.append(f"[{chunk_id}] {name}\n{chunk_path}")
    return "\n\n---\n\n".join(documents)


def _build_qa_messages(documents_text: str, query: str) -> List[Dict[str, str]]:
    """QA 처리용 메시지 구성"""
    return [{
        "role": ROLE_USER,
        "content": f"documents: {documents_text}\n\nuser_query: {query}"
    }]


def _create_llm_params(
    temperature: float = 0,
    max_tokens: int = None,
    stream: bool = False,
    frequency_penalty: float = 0,
    repetition_penalty: float = 1.0,
    top_p: float = 1.0,
    top_k: int = 1,
    seed: int = None,
    tools: list = None
) -> Dict[str, Any]:
    """LLM API 호출용 파라미터 객체 생성"""
    params = {
        "temperature": temperature,
        "stream": stream,
        "frequency_penalty": frequency_penalty,
        "repetition_penalty": repetition_penalty,
        "top_p": top_p,
        "top_k": top_k,
    }
    if max_tokens:
        params["max_tokens"] = max_tokens
    if seed is not None:
        params["seed"] = seed
    if tools is not None:
        params["tools"] = tools
    return params


def _is_sufficient(
    docs: List[Dict[str, Any]],
    min_count: int = 5,
    weight_threshold: float = 0.75,
) -> bool:
    """WEIGHT >= weight_threshold 인 문서가 min_count개 이상이면 충분하다고 판단"""
    qualified = [d for d in docs if float(d.get("WEIGHT", 0) or 0) >= weight_threshold]
    return len(qualified) >= min_count


def _deduplicate_documents(doc_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """문서 중복 제거 (CHUNK_ID 기준, 동일 CHUNK_ID 중 WEIGHT 최고값 유지)"""
    best: Dict[str, Dict[str, Any]] = {}
    no_id_docs = []
    for doc in doc_list:
        chunk_id = doc.get("CHUNK_ID")
        if not chunk_id:
            no_id_docs.append(doc)
            continue
        weight = float(doc.get("WEIGHT", 0) or 0)
        if chunk_id not in best or weight > float(best[chunk_id].get("WEIGHT", 0) or 0):
            best[chunk_id] = doc

    unique_docs = list(best.values()) + no_id_docs
    logger.info(f"[RAG] 중복 제거: {len(doc_list)} → {len(unique_docs)}개")
    return unique_docs


def _okms_dual_query_for_search(
    user_message: str,
    vector_q: str,
    keyword_q: str,
    *,
    policy_search_boost_enabled: bool,
) -> Tuple[str, str]:
    """Group A (vector, keyword) 쌍. MORE_INFO 등에서는 정책 키워드 보강 없이 원질의만 사용."""
    if not policy_search_boost_enabled:
        return (vector_q or "").strip(), (keyword_q or "").strip()
    return augment_okms_dual_query(user_message, vector_q, keyword_q or "")


def resolve_fallback_max_expanded_queries(
    user_message: str,
    *,
    default_max: int = FALLBACK_MAX_EXPANDED_DEFAULT,
    policy_search_boost_enabled: bool = True,
) -> int:
    """질문군별 fallback 확장 쿼리 상한을 반환."""
    if not policy_search_boost_enabled:
        return default_max
    tags, _ = resolve_policy_boost_keywords(user_message)
    if "elderly_benefits" in tags:
        return FALLBACK_MAX_EXPANDED_ELDERLY
    return default_max


def should_rerun_sufficiency_judgment(
    before_docs: List[Dict[str, Any]],
    after_docs: List[Dict[str, Any]],
    *,
    min_doc_gain: int = FALLBACK_REJUDGE_MIN_DOC_GAIN,
    min_top_weight_gain: float = FALLBACK_REJUDGE_MIN_TOP_WEIGHT_GAIN,
) -> bool:
    """fallback 후 재판단 필요 여부(문서 수/상위 weight 개선 기반)."""
    before_n = len(before_docs or [])
    after_n = len(after_docs or [])
    if (after_n - before_n) >= min_doc_gain:
        return True

    def _top_weight(docs: List[Dict[str, Any]]) -> float:
        if not docs:
            return 0.0
        return max(float(d.get("WEIGHT", 0) or 0) for d in docs)

    return (_top_weight(after_docs) - _top_weight(before_docs)) >= min_top_weight_gain


async def run_sufficiency_with_shortcut(
    *,
    judge_fn: Callable[..., Awaitable[Dict[str, Any]]],
    user_question: str,
    intent: str,
    collection_name: str,
    docs: List[Dict[str, Any]],
    sigun_filters: Optional[List[str]] = None,
    timeout_sec: float = SUFFICIENCY_JUDGMENT_TIMEOUT_SEC,
    max_docs: int = SUFFICIENCY_JUDGMENT_MAX_DOCS,
    log_prefix: str = "RAG",
) -> Dict[str, Any]:
    """휴리스틱 단축이 가능하면 LLM 호출 없이 즉시 충분 판정, 아니면 LLM 폴백.

    Config.RAG_SUFFICIENCY_FAST_PATH_ENABLED=False면 단축을 건너뛴다.
    """
    if Config.RAG_SUFFICIENCY_FAST_PATH_ENABLED:
        shortcut = shortcut_sufficiency_by_anchor(
            user_question=user_question,
            docs=docs,
            sigun_filters=sigun_filters,
            log_prefix=log_prefix,
        )
        if shortcut is not None:
            return shortcut

    return await run_sufficiency_judgment_fast(
        judge_fn=judge_fn,
        user_question=user_question,
        intent=intent,
        collection_name=collection_name,
        docs=docs,
        timeout_sec=timeout_sec,
        max_docs=max_docs,
        log_prefix=log_prefix,
    )


async def run_sufficiency_judgment_fast(
    *,
    judge_fn: Callable[..., Awaitable[Dict[str, Any]]],
    user_question: str,
    intent: str,
    collection_name: str,
    docs: List[Dict[str, Any]],
    timeout_sec: float = SUFFICIENCY_JUDGMENT_TIMEOUT_SEC,
    max_docs: int = SUFFICIENCY_JUDGMENT_MAX_DOCS,
    log_prefix: str = "RAG",
) -> Dict[str, Any]:
    """적합성 판단 호출을 타임아웃/문서수 cap으로 보호한다."""
    target_docs = list(docs or [])[:max_docs]
    if len(docs or []) > len(target_docs):
        logger.debug(
            "[%s] sufficiency docs cap: %d -> %d",
            log_prefix,
            len(docs or []),
            len(target_docs),
        )
    try:
        return await asyncio.wait_for(
            judge_fn(
                user_question=user_question,
                intent=intent,
                collection_name=collection_name,
                docs=target_docs,
            ),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        logger.warning("[%s] sufficiency timeout(%.1fs) -> graceful fallback", log_prefix, timeout_sec)
        return {"sufficient": False, "reason": "judgment_timeout"}
    except Exception as e:
        logger.warning("[%s] sufficiency error(%s) -> graceful fallback", log_prefix, e)
        return {"sufficient": False, "reason": "judgment_error"}


async def collect_okms_groupa_and_gov_docs(
    *,
    message: str,
    reformed_query: str,
    expanded_queries: List[str],
    tri_built: List[str],
    per_query_limit: int,
    run_group_a: Callable[[str, str], Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]],
    run_gov: Callable[[str], List[Dict[str, Any]]],
    log_prefix: str,
    status_callback: Callable[[str], Awaitable[None]] | None = None,
    log_skip_empty_triple: bool = False,
    policy_search_boost_enabled: bool = True,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """OKMS GroupA + GOV_OKMS 병렬 수집 공통 실행기."""
    loop = asyncio.get_event_loop()

    policy_extra_pairs = (
        policy_extra_okms_searches(message, reformed_query)
        if policy_search_boost_enabled
        else []
    )
    ga_pair_futures = [
        loop.run_in_executor(
            None,
            run_group_a,
            *_okms_dual_query_for_search(
                message, eq, sq if sq else "",
                policy_search_boost_enabled=policy_search_boost_enabled,
            ),
        )
        for eq, sq in zip_longest(expanded_queries, tri_built, fillvalue="")
    ]
    ga_policy_extra_futures = [
        loop.run_in_executor(None, run_group_a, v, k)
        for v, k in policy_extra_pairs
    ]

    gov_strings = list(expanded_queries) + [sq for sq in tri_built if sq]
    for vec_q, kw_q in policy_extra_pairs:
        gov_strings.append(vec_q)
        if kw_q:
            gov_strings.append(kw_q)
    gov_okms_futures = [loop.run_in_executor(None, run_gov, s) for s in gov_strings]

    if status_callback:
        await status_callback("문서를 검색하고 있습니다")
    all_results = await asyncio.gather(
        *ga_pair_futures,
        *ga_policy_extra_futures,
        *gov_okms_futures,
    )
    n_core_ga = len(ga_pair_futures)
    n_policy_x = len(ga_policy_extra_futures)
    ga_pair_results = all_results[:n_core_ga]
    ga_policy_extra_results = all_results[n_core_ga : n_core_ga + n_policy_x]
    gov_okms_results = all_results[n_core_ga + n_policy_x :]

    ga_vector_results = [pair[1] for pair in ga_pair_results]
    ga_keyword_results = [pair[0] for pair in ga_pair_results]

    okms_group_a_docs: List[Dict[str, Any]] = []
    for i, docs in enumerate(ga_vector_results, 1):
        if docs:
            okms_group_a_docs.extend(docs[:per_query_limit])
            logger.info("[%s] [GroupA] 확장쿼리 #%d: %d개 문서", log_prefix, i, min(len(docs), per_query_limit))
        else:
            logger.info("[%s] [GroupA] 확장쿼리 #%d: 0개 문서", log_prefix, i)

    for i, docs in enumerate(ga_keyword_results, 1):
        if not tri_built[i - 1]:
            if log_skip_empty_triple:
                logger.info("[%s] [GroupA] 트리플쿼리 #%d: 키워드 없음 - 건너뜀", log_prefix, i)
            continue
        if docs:
            okms_group_a_docs.extend(docs[:per_query_limit])
            logger.info("[%s] [GroupA] 트리플쿼리 #%d: %d개 문서", log_prefix, i, min(len(docs), per_query_limit))
        elif log_skip_empty_triple:
            logger.info("[%s] [GroupA] 트리플쿼리 #%d: 0개 문서", log_prefix, i)

    for pair in ga_policy_extra_results:
        if pair[1]:
            okms_group_a_docs.extend(pair[1][:per_query_limit])
        if pair[0]:
            okms_group_a_docs.extend(pair[0][:per_query_limit])
    if policy_extra_pairs:
        logger.debug("[%s] 정책 검색 보강: GroupA 추가 %d쌍", log_prefix, len(policy_extra_pairs))

    gov_okms_docs: List[Dict[str, Any]] = []
    for i, docs in enumerate(gov_okms_results, 1):
        if docs:
            gov_okms_docs.extend(docs[:per_query_limit])
            logger.debug("[%s] [GOV_OKMS] #%d: %d개 문서", log_prefix, i, min(len(docs), per_query_limit))
        else:
            logger.debug("[%s] [GOV_OKMS] #%d: 0개 문서", log_prefix, i)

    return okms_group_a_docs, gov_okms_docs


async def collect_okms_groupa_fallback_docs(
    *,
    message: str,
    reformed_query: str,
    expanded_queries: List[str],
    tri_built: List[str],
    per_query_limit: int,
    run_group_a_fallback: Callable[[str, str], Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]],
    max_policy_pairs: int = 1,
    policy_search_boost_enabled: bool = True,
) -> List[Dict[str, Any]]:
    """OKMS GroupA fallback 재검색 수집 공통 실행기."""
    loop = asyncio.get_event_loop()

    ga_fb_futures = [
        loop.run_in_executor(
            None,
            run_group_a_fallback,
            *_okms_dual_query_for_search(
                message, eq, sq if sq else "",
                policy_search_boost_enabled=policy_search_boost_enabled,
            ),
        )
        for eq, sq in zip_longest(expanded_queries, tri_built, fillvalue="")
    ]
    fb_policy_pairs = (
        policy_extra_okms_searches(message, reformed_query)[:max_policy_pairs]
        if policy_search_boost_enabled
        else []
    )
    ga_fb_extra_futures = [
        loop.run_in_executor(None, run_group_a_fallback, v, k)
        for v, k in fb_policy_pairs
    ]
    fb_results_all = await asyncio.gather(*ga_fb_futures, *ga_fb_extra_futures)
    ga_fb_results = fb_results_all[: len(ga_fb_futures)]
    ga_fb_extra_results = fb_results_all[len(ga_fb_futures) :]

    fb_docs: List[Dict[str, Any]] = []
    for i, pair in enumerate(ga_fb_results, 1):
        if pair[1]:
            fb_docs.extend(pair[1][:per_query_limit])
        if tri_built[i - 1] and pair[0]:
            fb_docs.extend(pair[0][:per_query_limit])
    for pair in ga_fb_extra_results:
        if pair[1]:
            fb_docs.extend(pair[1][:per_query_limit])
        if pair[0]:
            fb_docs.extend(pair[0][:per_query_limit])
    return fb_docs


async def collect_okms_groupa_and_gov_fallback_docs(
    *,
    message: str,
    reformed_query: str,
    expanded_queries: List[str],
    tri_built: List[str],
    per_query_limit: int,
    run_group_a: Callable[[str, str], Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]],
    run_gov: Callable[[str], List[Dict[str, Any]]],
    max_policy_pairs: int = 1,
    policy_search_boost_enabled: bool = True,
) -> List[Dict[str, Any]]:
    """OKMS fallback 보강검색(GroupA + GOV) 문서 수집 공통 실행기."""
    loop = asyncio.get_event_loop()

    fb_pairs = [
        _okms_dual_query_for_search(
            message, eq, sq if sq else "",
            policy_search_boost_enabled=policy_search_boost_enabled,
        )
        for eq, sq in zip_longest(expanded_queries, tri_built, fillvalue="")
    ]
    policy_pairs = (
        policy_extra_okms_searches(message, reformed_query)[:max_policy_pairs]
        if policy_search_boost_enabled
        else []
    )
    fb_pairs.extend(policy_pairs)

    fb_pair_futures = [
        loop.run_in_executor(None, run_group_a, vec, kw)
        for vec, kw in fb_pairs
    ]

    gov_queries = list(expanded_queries) + [sq for sq in tri_built if sq]
    for vec_q, kw_q in policy_pairs:
        gov_queries.append(vec_q)
        if kw_q:
            gov_queries.append(kw_q)
    fb_gov_futures = [
        loop.run_in_executor(None, run_gov, s)
        for s in gov_queries
    ]

    fb_results = await asyncio.gather(*fb_pair_futures, *fb_gov_futures)
    fb_pair_results = fb_results[:len(fb_pair_futures)]
    fb_gov_results = fb_results[len(fb_pair_futures):]

    fb_docs: List[Dict[str, Any]] = []
    for pair in fb_pair_results:
        if pair[1]:
            fb_docs.extend(pair[1][:per_query_limit])
        if pair[0]:
            fb_docs.extend(pair[0][:per_query_limit])
    for docs in fb_gov_results:
        if docs:
            fb_docs.extend(docs[:per_query_limit])
    return fb_docs


def build_welfare_search_queries(
    expanded_queries: List[str],
    search_queries: List[str],
    user_message: str,
    max_policy_queries: int | None = None,
    policy_search_boost_enabled: bool = True,
) -> Tuple[List[str], List[str]]:
    """search 의도 CENTER/TEL 공통 질의 목록 구성 + 정책 보강 질의 반환."""
    policy_queries = (
        policy_supplement_welfare_queries(user_message)
        if policy_search_boost_enabled
        else []
    )
    if max_policy_queries is not None:
        policy_queries = policy_queries[:max(0, max_policy_queries)]
    merged_parts = list(expanded_queries) + list(search_queries) + policy_queries

    seen_cf: set[str] = set()
    all_queries: List[str] = []
    for q in merged_parts:
        s = (q or "").strip()
        if not s:
            continue
        key = s.casefold()
        if key in seen_cf:
            continue
        seen_cf.add(key)
        all_queries.append(s)
    return all_queries, policy_queries


async def collect_welfare_center_tel_docs(
    *,
    queries: List[str],
    per_query_limit: int,
    run_center_query: Callable[[str], List[Dict[str, Any]]],
    run_tel_query: Callable[[str], List[Dict[str, Any]]],
    log_prefix: str = "RAG/search_v2",
    per_query_limit_tel: Optional[int] = None,
    center_enabled: bool = True,
    tel_enabled: bool = True,
    stage_timeout_sec: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """CENTER + TEL 병렬 검색 결과 수집.

    per_query_limit: 0 미만이면 CENTER는 쿼리별 반환 전량.
    per_query_limit_tel: None이면 TEL도 per_query_limit과 동일. 0 미만이면 TEL은 쿼리별 전량.
    center_enabled / tel_enabled: False이면 해당 풀은 검색 생략(빈 리스트).
    stage_timeout_sec: 지정 시 전체 수집 단계 시간 상한(초). 초과 시 남은 쿼리는 건너뛰고
        현재까지 수집된 부분 결과를 반환한다.
    """
    tel_cap = per_query_limit if per_query_limit_tel is None else per_query_limit_tel
    loop = asyncio.get_event_loop()
    stage_started_at = asyncio.get_running_loop().time()
    tel_results = [[] for _ in queries]
    tel_futures: Dict[asyncio.Future, int] = {}

    if tel_enabled and queries:
        # CENTER 수집이 느릴 때도 TEL 검색은 동시에 진행되도록 먼저 시작한다.
        tel_futures = {
            loop.run_in_executor(None, run_tel_query, q): idx
            for idx, q in enumerate(queries)
        }

    def _remaining_stage_sec() -> Optional[float]:
        if stage_timeout_sec is None:
            return None
        return max(stage_timeout_sec - (asyncio.get_running_loop().time() - stage_started_at), 0.0)

    if center_enabled and queries:
        # WELFARE_CENTER는 벡터·다필드 OR가 무거워 동시 다발 요청 시 Mariner 타임아웃(-60004)이 잦음 → 순차 실행
        center_results = [[] for _ in queries]
        for i, q in enumerate(queries):
            remaining = _remaining_stage_sec()
            if remaining is not None and remaining <= 0:
                logger.warning(
                    "[%s] 수집 단계 시간 상한(%.1fs) 초과로 CENTER 조기 중단",
                    log_prefix,
                    stage_timeout_sec,
                )
                break
            try:
                if remaining is None:
                    docs = await loop.run_in_executor(None, run_center_query, q)
                else:
                    docs = await asyncio.wait_for(
                        loop.run_in_executor(None, run_center_query, q),
                        timeout=remaining,
                    )
                center_results[i] = docs
            except asyncio.TimeoutError:
                logger.warning(
                    "[%s] 수집 단계 시간 상한(%.1fs) 초과로 CENTER 조기 중단",
                    log_prefix,
                    stage_timeout_sec,
                )
                break
    else:
        center_results = [[] for _ in queries]

    if tel_enabled and queries:
        pending = set(tel_futures.keys())
        while pending:
            remaining = _remaining_stage_sec()
            if remaining is not None and remaining <= 0:
                logger.warning(
                    "[%s] 수집 단계 시간 상한(%.1fs) 초과로 TEL 조기 중단",
                    log_prefix,
                    stage_timeout_sec,
                )
                break
            done, pending = await asyncio.wait(
                pending,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                logger.warning(
                    "[%s] 수집 단계 시간 상한(%.1fs) 초과로 TEL 조기 중단",
                    log_prefix,
                    stage_timeout_sec,
                )
                break
            for fut in done:
                idx = tel_futures.get(fut)
                if idx is None:
                    continue
                try:
                    tel_results[idx] = fut.result()
                except Exception:
                    tel_results[idx] = []
        for fut in pending:
            fut.cancel()

    center_docs: List[Dict[str, Any]] = []
    if center_enabled:
        for i, (q, docs) in enumerate(zip(queries, center_results), 1):
            if docs:
                if per_query_limit < 0:
                    center_docs.extend(docs)
                    n_take = len(docs)
                else:
                    center_docs.extend(docs[:per_query_limit])
                    n_take = min(len(docs), per_query_limit)
                logger.debug("[%s] [CENTER] 쿼리 #%d '%s': %d개", log_prefix, i, q[:30], n_take)
            else:
                logger.debug("[%s] [CENTER] 쿼리 #%d '%s': 0개", log_prefix, i, q[:30])
    else:
        logger.debug(
            "[%s] [CENTER] 검색 생략(단일 풀 모드: OUR_REGION_TEL 등에서 CENTER 풀 비활성)",
            log_prefix,
        )

    tel_docs: List[Dict[str, Any]] = []
    for i, (q, docs) in enumerate(zip(queries, tel_results), 1):
        if docs:
            if tel_cap < 0:
                tel_docs.extend(docs)
                n_take = len(docs)
            else:
                tel_docs.extend(docs[:tel_cap])
                n_take = min(len(docs), tel_cap)
            logger.debug("[%s] [TEL] 쿼리 #%d '%s': %d개", log_prefix, i, q[:30], n_take)
        else:
            logger.debug("[%s] [TEL] 쿼리 #%d '%s': 0개", log_prefix, i, q[:30])
    return center_docs, tel_docs


