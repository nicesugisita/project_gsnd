"""RAG 파이프라인 공통 유틸리티 함수"""

import asyncio
import logging
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


# GOV_OKMS 검색 문자열에서 제거할 광범위 일반어.
# GOV_OKMS WHERE 의 SERVICE_NAME_KO OP_HASANY(weight=0.7) 가 한 토큰이라도 매칭되면 boost 하므로,
# "복지/지원/서비스" 같은 거의 모든 복지 서비스명에 들어 있는 토큰을 그대로 두면
# 무관 서비스가 광범위 매칭됨(예: "창원 노인 복지 추천" → "청소년**복지**시설" 매칭).
# OKMS 는 sigun 필터로 범위가 좁혀지지만 GOV_OKMS 는 전국 DB라 노이즈가 큰 문제가 됨.
_GOV_OKMS_KEYWORD_STOPWORDS: frozenset[str] = frozenset({
    "복지",
    "지원",
    "서비스",
    "사업",
    "정책",
    "안내",
    "프로그램",
    "추천",
    "혜택",
})


def _filter_gov_okms_stopwords(query: str) -> str:
    """공백 분리된 토큰 중 일반어를 제거. 전부 제거되면 원본 반환(검색 신호 보존)."""
    raw = (query or "").strip()
    if not raw:
        return raw
    tokens = raw.split()
    filtered = [t for t in tokens if t and t not in _GOV_OKMS_KEYWORD_STOPWORDS]
    if not filtered:
        return raw
    return " ".join(filtered)


def filter_gov_okms_docs_by_lifecycle(
    docs: List[Dict[str, Any]],
    user_lifecycle: Optional[str],
) -> List[Dict[str, Any]]:
    """GOV_OKMS 결과를 LIFE_CYCLE 필드 기준으로 post-filter.

    배경: GOV_OKMS WHERE 의 LIFE_CYCLE 필터(op code 34)가 정의된 OP 상수 목록에 없는
    undocumented 연산자라 동작이 일관적이지 않음(예: 노년 필터에 "아동, 청년, 청소년"
    문서가 통과되는 사례 확인). DB 필드를 직접 파싱해 결정적으로 검증한다.

    동작:
    - user_lifecycle 가 빈값/None: 사용자 질의에서 생애주기 단서가 추출되지 않은
      경우이므로 **필터 없이 원본 반환** (false negative 방지).
    - user_lifecycle 지정: GOV_OKMS 표준값으로 매핑(예: "노인" → "노년") 후,
      문서 LIFE_CYCLE 멀티값(쉼표·세미콜론·슬래시 구분)에 포함되어야 유지.
    - 문서 LIFE_CYCLE 필드 자체가 빈값: 전체 대상 문서일 수 있어 **보수적으로 유지**.
    """
    if not user_lifecycle:
        return docs
    if not docs:
        return docs

    # 매핑은 queryset_gov_okms 의 SSOT 사용 (lazy import 로 순환 회피)
    try:
        from app.mariner.queryset_gov_okms import _map_lifecycle_for_gov_okms
        target = (_map_lifecycle_for_gov_okms(user_lifecycle) or "").strip()
    except Exception:
        target = str(user_lifecycle or "").strip()
    if not target:
        return docs

    kept: List[Dict[str, Any]] = []
    removed_samples: List[str] = []
    def _norm_lc(s: str) -> str:
        # 가운데점(·) 주변 공백 변형을 통일: "임신 · 출산" / "임신· 출산" → "임신·출산"
        # 일반 공백/탭/NBSP 모두 제거 후 가운데점 표준화.
        return s.replace(" ", "").replace(" ", "").replace("\t", "").strip()

    target_norm = _norm_lc(target)
    for doc in docs:
        raw_field = str(doc.get("LIFE_CYCLE", "") or "").strip()
        if not raw_field:
            # LIFE_CYCLE 빈값: 전체 대상 가능성 → 유지
            kept.append(doc)
            continue
        # 멀티값 토큰화: "아동,청년,청소년" / "아동;청년" / "아동/청년" / "아동 청년"
        # 모두 동일하게 분해
        normalized = raw_field.replace(";", ",").replace("/", ",")
        tokens = [t.strip() for t in normalized.split(",") if t.strip()]
        # 공백 구분 케이스도 보강 (가운데점 변형 케이스 제외).
        # "아동 청년"처럼 콤마/슬래시 없이 공백으로 구분된 경우만 split.
        if len(tokens) == 1 and " " in tokens[0] and "·" not in tokens[0]:
            tokens = [t.strip() for t in tokens[0].split() if t.strip()]
        # 가운데점 주변 공백 통일 후 비교
        normalized_tokens = [_norm_lc(t) for t in tokens]
        if target_norm in normalized_tokens:
            kept.append(doc)
        else:
            if len(removed_samples) < 5:
                _name = (
                    str(doc.get("NAME", "") or "").strip()
                    or str(doc.get("SERVICE_NAME", "") or "").strip()
                    or str(doc.get("CHUNK_ID", "") or "").strip()
                    or "?"
                )
                removed_samples.append(f"{_name}(LIFE_CYCLE={raw_field})")

    if len(kept) < len(docs):
        logger.info(
            "[GOV_OKMS][LifecyclePostFilter] target=%s | 입력=%d → %d 유지 (제거 %d건, 샘플=%s)",
            target,
            len(docs),
            len(kept),
            len(docs) - len(kept),
            removed_samples,
        )
    return kept


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


def prioritize_general_household(
    pool: List[Dict[str, Any]],
    target: int,
    min_keep: int,
    *,
    require_general: bool = True,
    lifecycle: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """가구상황·생애주기 적합 문서를 우선하고 부적합 문서를 제외하는 최종 후처리.

    - require_general=True(기본 일반가구 질의): HOUSE_SITUATION 에 '일반가구' 토큰이 없는
      특정계층(저소득/다문화·탈북민 등) '단독' 태그 제도를 제외.
    - lifecycle 지정 시: LIFE_CYCLE 에 해당 생애주기(예: '아동')를 포함하지 않는 문서를 제외
      (예: '초등학생' 질의에 '청소년' 단독 대상 '고등학교 무상교육' 이 섞이는 것 방지).
      멀티값('아동,청소년')은 포함하므로 유지된다.

    둘 다 만족(적합)하는 문서를 WEIGHT 순으로 target 까지 채우고, 적합 문서가 min_keep 미달이면
    부적합 문서 중 WEIGHT 상위로 백필해 답변이 3~4건으로 쪼그라드는 것을 막는다.

    Args:
        pool: 후보 문서 (중복 제거 권장). HOUSE_SITUATION / LIFE_CYCLE / WEIGHT 보유.
        target: 적합 문서를 채울 상한 (예: 8).
        min_keep: 최소 보장 건수 — 적합 문서가 이보다 적으면 부적합분에서 백필.
        require_general: 일반가구 태그 요구 여부 (명시 가구상황 질의면 False).
        lifecycle: 요구 생애주기 ('' / None 이면 생애주기 미적용).
    """
    def _w(d: Dict[str, Any]) -> float:
        try:
            return float(d.get("WEIGHT", 0) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _fit(d: Dict[str, Any]) -> bool:
        if require_general and "일반가구" not in str(d.get("HOUSE_SITUATION", "") or ""):
            return False
        if lifecycle and lifecycle not in str(d.get("LIFE_CYCLE", "") or ""):
            return False
        return True

    fit = sorted([d for d in pool if _fit(d)], key=_w, reverse=True)
    unfit = sorted([d for d in pool if not _fit(d)], key=_w, reverse=True)

    result = fit[:target]
    if len(result) < min_keep:
        result += unfit[: max(0, min_keep - len(result))]
    return result


def _okms_dual_query_for_search(
    vector_q: str,
    keyword_q: str,
    *,
    policy_priority_tag: Optional[str],
    policy_search_boost_enabled: bool,
) -> Tuple[str, str]:
    """Group A (vector, keyword) 쌍. MORE_INFO 등에서는 정책 키워드 보강 없이 원질의만 사용."""
    if not policy_search_boost_enabled:
        return (vector_q or "").strip(), (keyword_q or "").strip()
    return augment_okms_dual_query(policy_priority_tag, vector_q, keyword_q or "")


async def collect_okms_groupa_and_gov_docs(
    *,
    message: str,
    reformed_query: str,
    policy_priority_tag: Optional[str],
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
        policy_extra_okms_searches(policy_priority_tag, reformed_query)
        if policy_search_boost_enabled
        else []
    )
    ga_pair_futures = [
        loop.run_in_executor(
            None,
            run_group_a,
            *_okms_dual_query_for_search(
                eq, sq if sq else "",
                policy_priority_tag=policy_priority_tag,
                policy_search_boost_enabled=policy_search_boost_enabled,
            ),
        )
        for eq, sq in zip_longest(expanded_queries, tri_built, fillvalue="")
    ]
    ga_policy_extra_futures = [
        loop.run_in_executor(None, run_group_a, v, k)
        for v, k in policy_extra_pairs
    ]

    # GOV_OKMS는 핵심어(tri_built)만 사용한다.
    # - expanded_queries 제외: "복지", "지원" 같은 공통 토큰이 OP_HASANY로 매칭돼
    #   치매 질의에 산림복지·장애인지원 같은 무관 서비스가 상위 차지하는 오매칭 방지.
    # - policy_extra anchor 제외: GOV_OKMS는 sigun 필터 없는 전국 DB라
    #   "기초연금" 같은 anchor가 토크나이저에서 「연금」 토큰으로 분해돼
    #   농업인연금/국민연금 등 무관 서비스를 광범위하게 매칭시킴.
    #   정책 boost 는 sigun 필터로 범위가 좁혀지는 OKMS 에서만 적용.
    # - stopword 제거: 광범위 일반어("복지/지원/서비스" 등)는 OP_HASANY 노이즈 유발 →
    #   GOV_OKMS 전송 직전에 제거.
    _raw_gov_strings = [sq for sq in tri_built if sq]
    if not _raw_gov_strings and reformed_query.strip():
        _raw_gov_strings = [reformed_query.strip()]
    gov_strings = [_filter_gov_okms_stopwords(s) for s in _raw_gov_strings]
    gov_strings = [s for s in gov_strings if s]
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
    policy_priority_tag: Optional[str],
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
                eq, sq if sq else "",
                policy_priority_tag=policy_priority_tag,
                policy_search_boost_enabled=policy_search_boost_enabled,
            ),
        )
        for eq, sq in zip_longest(expanded_queries, tri_built, fillvalue="")
    ]
    fb_policy_pairs = (
        policy_extra_okms_searches(policy_priority_tag, reformed_query)[:max_policy_pairs]
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
    policy_priority_tag: Optional[str],
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
            eq, sq if sq else "",
            policy_priority_tag=policy_priority_tag,
            policy_search_boost_enabled=policy_search_boost_enabled,
        )
        for eq, sq in zip_longest(expanded_queries, tri_built, fillvalue="")
    ]
    policy_pairs = (
        policy_extra_okms_searches(policy_priority_tag, reformed_query)[:max_policy_pairs]
        if policy_search_boost_enabled
        else []
    )
    fb_pairs.extend(policy_pairs)

    fb_pair_futures = [
        loop.run_in_executor(None, run_group_a, vec, kw)
        for vec, kw in fb_pairs
    ]

    # GOV_OKMS는 핵심어(tri_built)만 사용한다.
    # - expanded_queries 제외 / policy_extra anchor 제외 (전국 DB 광범위 매칭 방지).
    # - stopword 제거: 일반어 OP_HASANY 노이즈 차단.
    # - 정책 boost 는 sigun 필터가 있는 OKMS 검색에서만 효과적.
    _raw_gov_queries = [sq for sq in tri_built if sq]
    if not _raw_gov_queries and reformed_query.strip():
        _raw_gov_queries = [reformed_query.strip()]
    gov_queries = [_filter_gov_okms_stopwords(s) for s in _raw_gov_queries]
    gov_queries = [s for s in gov_queries if s]
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
    policy_priority_tag: Optional[str],
    max_policy_queries: int | None = None,
    policy_search_boost_enabled: bool = True,
) -> Tuple[List[str], List[str]]:
    """search 의도 CENTER/TEL 공통 질의 목록 구성 + 정책 보강 질의 반환."""
    policy_queries = (
        policy_supplement_welfare_queries(policy_priority_tag)
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


