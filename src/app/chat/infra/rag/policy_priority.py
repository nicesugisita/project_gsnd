"""질문군별 핵심 서비스 soft-priority — common/response_generator와 순환 없이 공유."""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, FrozenSet, List, Tuple

from app.core.config import Config
from app.shared.db.connection import get_db_connection

logger = logging.getLogger(__name__)

_DEFAULT_TAG_KEYWORDS: Dict[str, Tuple[str, ...]] = {}
# 하드코딩 기본 키워드 비활성화(요청 반영)
# DB(gsnd_policy_priority)에서만 정책 키워드를 로딩한다.
#
# _DEFAULT_TAG_KEYWORDS: Dict[str, Tuple[str, ...]] = {
#     "implant": ("임플란트", "치과", "구강", "의료급여"),
#     "low_income": ("생계급여", "의료급여", "저소득", "기초생활"),
#     "elderly_benefits": (
#         "기초연금",
#         "기초 연금",
#         "노인맞춤돌봄",
#         "노인 맞춤돌봄",
#         "맞춤돌봄",
#         "돌봄서비스",
#     ),
# }

_TAG_KEYWORDS_CACHE: Dict[str, Tuple[str, ...]] = dict(_DEFAULT_TAG_KEYWORDS)
_TAG_KEYWORDS_CACHE_AT = 0.0
_TAG_KEYWORDS_LAST_CHECK_AT = 0.0
_TAG_KEYWORDS_LAST_VERSION: str = ""
_TAG_KEYWORDS_CACHE_READY = False
_CACHE_LOCK = threading.Lock()
_SAFE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REFRESH_STOP_EVENT = threading.Event()
_REFRESH_THREAD: threading.Thread | None = None


def _safe_table_name_or_none(name: str) -> str | None:
    raw = (name or "").strip()
    if not raw:
        return None
    if not _SAFE_IDENT_RE.fullmatch(raw):
        logger.warning("[PolicyBoost] invalid POLICY_PRIORITY_TABLE=%r; fallback to defaults", raw)
        return None
    return raw


def _load_policy_keywords_from_db() -> Dict[str, Tuple[str, ...]]:
    """DB에서 질문군별 우선순위 키워드를 읽어온다."""
    if not Config.POLICY_PRIORITY_DB_ENABLED:
        return {}

    table = _safe_table_name_or_none(Config.POLICY_PRIORITY_TABLE)
    if not table:
        return {}

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        # 기대 스키마:
        # - policy_tag: 질문군 태그 (implant/low_income/elderly_benefits)
        # - keyword: 부스트 키워드
        # - priority_order: 오름차순 우선순위
        # - is_active: 사용 여부(1/0)
        cur.execute(
            f"""
            SELECT policy_tag, keyword
            FROM {table}
            WHERE is_active = 1
            ORDER BY policy_tag ASC, priority_order ASC, keyword ASC
            """
        )
        rows = cur.fetchall() or []
    except Exception as e:
        logger.warning("[PolicyBoost] db load failed: %s", e)
        return {}
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass

    by_tag: Dict[str, List[str]] = {}
    for row in rows:
        tag = str(row.get("policy_tag", "") or "").strip()
        kw = str(row.get("keyword", "") or "").strip()
        if not tag or not kw:
            continue
        by_tag.setdefault(tag, []).append(kw)

    resolved: Dict[str, Tuple[str, ...]] = {}
    for tag, kws in by_tag.items():
        seen: set[str] = set()
        ordered: List[str] = []
        for kw in kws:
            kcf = kw.casefold()
            if kcf in seen:
                continue
            seen.add(kcf)
            ordered.append(kw)
        if ordered:
            resolved[tag] = tuple(ordered)
    return resolved


def _load_policy_keywords_version_from_db() -> str:
    """DB의 정책 키워드 최신 버전(MAX(updated_at))을 조회한다."""
    if not Config.POLICY_PRIORITY_DB_ENABLED:
        return ""

    table = _safe_table_name_or_none(Config.POLICY_PRIORITY_TABLE)
    if not table:
        return ""

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT COALESCE(DATE_FORMAT(MAX(updated_at), '%Y-%m-%d %H:%i:%s'), '')
            FROM {table}
            WHERE is_active = 1
            """
        )
        row = cur.fetchone()
        if not row:
            return ""
        return str(row[0] or "").strip()
    except Exception as e:
        logger.warning("[PolicyBoost] version check failed: %s", e)
        return ""
    finally:
        try:
            if cur is not None:
                cur.close()
        except Exception:
            pass
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _get_tag_keywords_map() -> Dict[str, Tuple[str, ...]]:
    """질문군별 키워드 맵(캐시 + 24시간 주기 변경 체크 + 조건부 재조회)."""
    ttl = max(int(Config.POLICY_PRIORITY_CACHE_TTL_SEC or 0), 0)
    check_interval = max(int(Config.POLICY_PRIORITY_CHANGE_CHECK_SEC or 0), 0)
    now = time.monotonic()
    with _CACHE_LOCK:
        global _TAG_KEYWORDS_CACHE_AT, _TAG_KEYWORDS_CACHE
        global _TAG_KEYWORDS_LAST_CHECK_AT, _TAG_KEYWORDS_LAST_VERSION
        global _TAG_KEYWORDS_CACHE_READY

        has_cache = _TAG_KEYWORDS_CACHE_READY
        if has_cache and check_interval > 0 and _TAG_KEYWORDS_LAST_CHECK_AT:
            if (now - _TAG_KEYWORDS_LAST_CHECK_AT) < check_interval:
                return _TAG_KEYWORDS_CACHE

        # 초기 구동/캐시 미존재 시에는 기존 TTL 정책으로 1회 로딩
        if not has_cache and ttl > 0 and _TAG_KEYWORDS_CACHE_AT and (now - _TAG_KEYWORDS_CACHE_AT) < ttl:
            return _TAG_KEYWORDS_CACHE

        # 24시간 주기 변경 체크: 버전이 동일하면 캐시 유지
        if has_cache and check_interval > 0:
            logger.info(
                "[PolicyBoost] change-check start: interval=%ss, last_check_ago=%.1fs",
                check_interval,
                now - _TAG_KEYWORDS_LAST_CHECK_AT if _TAG_KEYWORDS_LAST_CHECK_AT else -1.0,
            )
            current_version = _load_policy_keywords_version_from_db()
            _TAG_KEYWORDS_LAST_CHECK_AT = now
            if current_version and current_version == _TAG_KEYWORDS_LAST_VERSION:
                logger.info(
                    "[PolicyBoost] change-check no-change: version=%s",
                    current_version,
                )
                return _TAG_KEYWORDS_CACHE
            logger.info(
                "[PolicyBoost] change-check changed: old=%s new=%s -> reload",
                _TAG_KEYWORDS_LAST_VERSION or "(empty)",
                current_version or "(empty)",
            )

        db_map = _load_policy_keywords_from_db()
        merged = dict(_DEFAULT_TAG_KEYWORDS)
        for tag, kws in db_map.items():
            if kws:
                merged[tag] = kws
        _TAG_KEYWORDS_CACHE = merged
        _TAG_KEYWORDS_CACHE_AT = now
        _TAG_KEYWORDS_LAST_CHECK_AT = now
        _TAG_KEYWORDS_LAST_VERSION = _load_policy_keywords_version_from_db()
        _TAG_KEYWORDS_CACHE_READY = True
        logger.info(
            "[PolicyBoost] cache refreshed: tags=%d version=%s",
            len(_TAG_KEYWORDS_CACHE),
            _TAG_KEYWORDS_LAST_VERSION or "(empty)",
        )
        return _TAG_KEYWORDS_CACHE


def preload_policy_priority_cache() -> None:
    """앱 시작 시 정책 키워드 캐시를 즉시 채운다."""
    try:
        _get_tag_keywords_map()
    except Exception as e:
        logger.warning("[PolicyBoost] preload failed: %s", e)


def start_policy_priority_refresh_worker() -> None:
    """정책 키워드 캐시 변경 체크 워커를 시작한다."""
    global _REFRESH_THREAD

    interval = max(int(Config.POLICY_PRIORITY_CHANGE_CHECK_SEC or 0), 0)
    if interval <= 0:
        logger.info("[PolicyBoost] refresh worker disabled (interval=%s)", interval)
        return
    if not Config.POLICY_PRIORITY_DB_ENABLED:
        logger.info("[PolicyBoost] refresh worker skipped (db disabled)")
        return
    if _REFRESH_THREAD and _REFRESH_THREAD.is_alive():
        return

    _REFRESH_STOP_EVENT.clear()

    def _worker() -> None:
        logger.info("[PolicyBoost] refresh worker started (interval=%ss)", interval)
        while not _REFRESH_STOP_EVENT.wait(interval):
            try:
                # 요청 유입이 없어도 주기적으로 change-check를 수행한다.
                _get_tag_keywords_map()
            except Exception as e:
                logger.warning("[PolicyBoost] refresh worker error: %s", e)
        logger.info("[PolicyBoost] refresh worker stopped")

    _REFRESH_THREAD = threading.Thread(
        target=_worker,
        name="policy-priority-refresh-worker",
        daemon=True,
    )
    _REFRESH_THREAD.start()


def stop_policy_priority_refresh_worker() -> None:
    """정책 키워드 캐시 변경 체크 워커를 중지한다."""
    global _REFRESH_THREAD
    if not _REFRESH_THREAD:
        return
    _REFRESH_STOP_EVENT.set()
    _REFRESH_THREAD.join(timeout=2.0)
    _REFRESH_THREAD = None


def _resolve_keywords_for_tags(tags: FrozenSet[str]) -> Tuple[str, ...]:
    if not tags:
        return ()
    mapping = _get_tag_keywords_map()
    merged: List[str] = []
    seen: set[str] = set()
    for tag in sorted(tags):
        for kw in mapping.get(tag, ()):
            k = kw.strip()
            if not k:
                continue
            kcf = k.casefold()
            if kcf in seen:
                continue
            seen.add(kcf)
            merged.append(k)
    return tuple(merged)


def _pick_distinct_keywords(keywords: Tuple[str, ...], *, n: int) -> List[str]:
    """공백/대소문자 변형을 제외하고 서로 다른 키워드 n개를 고른다."""
    picked: List[str] = []
    seen_norm: set[str] = set()
    for kw in keywords:
        k = (kw or "").strip()
        if not k:
            continue
        norm = re.sub(r"\s+", "", k).casefold()
        if norm in seen_norm:
            continue
        seen_norm.add(norm)
        picked.append(k)
        if len(picked) >= n:
            break
    return picked


def _elderly_benefits_heuristic(message: str) -> bool:
    """노인·고령 대상 혜택/지원 질의 여부(키워드 휴리스틱)."""
    m = message.strip()
    if not m:
        return False
    age_or_elder = any(
        x in m
        for x in ("70", "칠십", "노인", "어르신", "부모", "고령")
    )
    benefit_ctx = any(
        x in m
        for x in ("혜택", "지원", "돌봄", "연금", "급여", "복지")
    )
    return age_or_elder and benefit_ctx


def resolve_policy_boost_keywords(message: str) -> Tuple[FrozenSet[str], Tuple[str, ...]]:
    """질문에 맞는 정책 태그와, 문서 매칭용 부스트 키워드 튜플을 반환한다.

    우선순위: 임플란트 > 저소득 > 노인혜택 (동시에 걸릴 때 고객 예시 기준).
    """
    msg = message.strip()
    if not msg:
        return frozenset(), ()

    if "임플란트" in msg:
        tags = frozenset({"implant"})
        return tags, _resolve_keywords_for_tags(tags)

    if any(k in msg for k in ("저소득", "생계급여", "의료급여")):
        tags = frozenset({"low_income"})
        return tags, _resolve_keywords_for_tags(tags)

    if _elderly_benefits_heuristic(msg):
        # 기초연금은 국가 기본 소득보장이라 노인 혜택 질의에서 다른 시군 사업.hwpx 들보다
        # 먼저 설명되는 것이 자연스럽다. apply_policy 에서 별도 1티어로 올린다.
        tags = frozenset({"elderly_benefits"})
        return tags, _resolve_keywords_for_tags(tags)

    return frozenset(), ()


def augment_okms_dual_query(
    user_message: str,
    vector_q: str,
    keyword_q: str,
) -> Tuple[str, str]:
    """Mariner Group A 검색 직전 (vector, keyword) 보강.

    트리플이 비어 있던 행을 건드리며 keyword 레그만 채우면, 수집 루프의 tri_built
    인덱스와 맞지 않아 키워드 결과가 버려질 수 있으므로, keyword 보강은 기존
    트리플 문자열이 있을 때만 한다. 빈 트리플 레그 보강은 `policy_extra_okms_searches`.
    """
    tags, kws = resolve_policy_boost_keywords(user_message)
    vec = (vector_q or "").strip()
    kw = (keyword_q or "").strip()
    if not tags:
        return vec, kw

    low_vec = vec.casefold()
    if "elderly_benefits" in tags:
        for needle in ("기초연금", "노인맞춤돌봄"):
            if needle.casefold() not in low_vec:
                vec = f"{vec} {needle}".strip()
                low_vec = vec.casefold()
    elif "implant" in tags:
        if "임플란트" not in low_vec:
            vec = f"{vec} 임플란트".strip()
            low_vec = vec.casefold()
    elif "low_income" in tags:
        for needle in ("생계급여", "의료급여"):
            if needle.casefold() not in low_vec:
                vec = f"{vec} {needle}".strip()
                low_vec = vec.casefold()

    if kw and kws:
        kw_cf = kw.casefold()
        extra = [
            t
            for t in kws
            if t.strip() and t.strip().casefold() not in kw_cf
        ]
        if extra:
            kw = f"{kw} {' '.join(extra)}".strip()

    return vec, kw


def policy_extra_okms_searches(user_message: str, reformed_query: str) -> List[Tuple[str, str]]:
    """정책 태그별 OKMS Group A 추가 검색 (vector, keyword) 쌍 — 빈 트리플·약한 검색 보강."""
    tags, _ = resolve_policy_boost_keywords(user_message)
    rq = (reformed_query or "").strip()
    tag_keywords = _get_tag_keywords_map()
    if not tags or not rq:
        return []
    if "elderly_benefits" in tags:
        elderly_kws = tag_keywords.get("elderly_benefits", ())
        anchors = _pick_distinct_keywords(elderly_kws, n=2)
        anchor_1 = anchors[0] if len(anchors) >= 1 else "기초연금"
        anchor_2 = anchors[1] if len(anchors) >= 2 else "노인맞춤돌봄"
        return [
            (f"{rq} {anchor_1} 안내", anchor_1),
            (f"{rq} {anchor_2}", anchor_2),
        ]
    if "implant" in tags:
        implant_kws = tag_keywords.get("implant", ())
        implant_kw = implant_kws[0] if implant_kws else "임플란트"
        return [(f"{rq} {implant_kw} 지원", implant_kw)]
    if "low_income" in tags:
        low_income_kws = tag_keywords.get("low_income", ())
        key_1 = low_income_kws[0] if len(low_income_kws) >= 1 else "생계급여"
        key_2 = low_income_kws[1] if len(low_income_kws) >= 2 else "의료급여"
        return [(f"{rq} {key_1} {key_2}", f"{key_1} {key_2}")]
    return []


def policy_supplement_welfare_queries(user_message: str) -> List[str]:
    """search 의도 WELFARE_CENTER/TEL 검색에 추가로 던질 짧은 쿼리."""
    tags, _ = resolve_policy_boost_keywords(user_message)
    tag_keywords = _get_tag_keywords_map()
    if "elderly_benefits" in tags:
        elderly = tag_keywords.get("elderly_benefits", ())
        return _pick_distinct_keywords(elderly, n=2)
    if "implant" in tags:
        implant = list(tag_keywords.get("implant", ()))
        return implant[:2]
    if "low_income" in tags:
        low_income = list(tag_keywords.get("low_income", ()))
        return low_income[:2]
    return []


def _document_policy_match_blob(doc: Dict[str, Any]) -> str:
    """정책 키워드 매칭용 텍스트(소문자)."""
    parts: List[str] = []
    for k in (
        "NAME",
        "BUSINESS_NAME",
        "ORG_NM",
        "FACILITY_NAME",
        "PURPOSE",
        "CONTENT",
        "CHUNK_PATH",
        "APPLICATION_PERIOD",
        "FACILITY_TYPE",
    ):
        parts.append(str(doc.get(k, "") or ""))
    return " ".join(parts).casefold()


def apply_policy_priority_to_documents(
    user_message: str,
    docs: List[Dict[str, Any]],
    *,
    log_prefix: str = "PolicyBoost",
) -> List[Dict[str, Any]]:
    """검색 후 문서 목록에 질문군별 soft-priority를 적용해 재정렬한다.

    - 문서 본문/제목에 정책 키워드가 포함된 후보를 앞으로 올린다.
    - 어느 문서에도 키워드가 없으면 원 순서를 유지한다(불필요한 순서 뒤집음 방지).
    - 동점 시 CHUNK_ID/ID 문자열로 안정 정렬.
    """
    if not docs:
        return docs

    tags, boost_keywords = resolve_policy_boost_keywords(user_message)
    if not boost_keywords:
        return docs

    kws_cf = tuple(kw.casefold() for kw in boost_keywords if kw.strip())
    _basic_pen_cf = frozenset({"기초연금".casefold(), "기초 연금".casefold()})

    def _hit_count(blob: str) -> int:
        return sum(1 for kw in kws_cf if kw in blob)

    def _elderly_sort_key(blob: str) -> Tuple[int, int]:
        """(기초연금·기초 연금 포함 여부, 나머지 부스트 키 적중 수)."""
        has_basic = any(p in blob for p in _basic_pen_cf)
        sec = sum(
            1
            for kw in kws_cf
            if kw not in _basic_pen_cf and kw in blob
        )
        return (1 if has_basic else 0, sec)

    blobs = [_document_policy_match_blob(d) for d in docs]

    if "elderly_benefits" in tags:
        max_basic, max_sec = 0, 0
        for b in blobs:
            prim, sec = _elderly_sort_key(b)
            max_basic = max(max_basic, prim)
            max_sec = max(max_sec, sec)
        if max_basic == 0 and max_sec == 0:
            logger.info("[%s] tags=%s — 매칭 문서 없음, 순서 유지", log_prefix, sorted(tags))
            return docs
        scored: List[Tuple[int, int, float, str, Dict[str, Any]]] = []
        for d, blob in zip(docs, blobs):
            prim, sec = _elderly_sort_key(blob)
            w = float(d.get("WEIGHT", 0) or 0)
            cid = str(d.get("CHUNK_ID", "") or d.get("ID", "") or "")
            scored.append((prim, sec, w, cid, d))
        scored.sort(key=lambda t: (-t[0], -t[1], -t[2], t[3]))
        reordered = [t[4] for t in scored]
        logger.info(
            "[%s] tags=%s — 노인혜택: 기초연금 1티어 후 나머지 키 조합 (boost_keys=%s)",
            log_prefix,
            sorted(tags),
            list(boost_keywords),
        )
        return reordered

    max_hits = max((_hit_count(b) for b in blobs), default=0)
    if max_hits == 0:
        logger.info("[%s] tags=%s — 매칭 문서 없음, 순서 유지", log_prefix, sorted(tags))
        return docs

    scored_kw: List[Tuple[int, float, str, Dict[str, Any]]] = []
    for d, blob in zip(docs, blobs):
        hits = _hit_count(blob)
        w = float(d.get("WEIGHT", 0) or 0)
        cid = str(d.get("CHUNK_ID", "") or d.get("ID", "") or "")
        scored_kw.append((hits, w, cid, d))

    scored_kw.sort(key=lambda t: (-t[0], -t[1], t[2]))

    reordered = [t[3] for t in scored_kw]
    logger.info(
        "[%s] tags=%s max_hits=%d — 정책 우선 재정렬 적용 (boost_keys=%s)",
        log_prefix,
        sorted(tags),
        max_hits,
        list(boost_keywords),
    )
    return reordered


def soft_priority_instruction_for_prompt(user_question: str) -> str:
    """최종 LLM user 메시지에 붙일 짧은 우선순위 안내(문서 근거만).

    주의:
    - 특정 키워드 목록을 그대로 노출하면 모델이 '키워드 부재' 안내로 치우칠 수 있어
      태그만 전달하고, '있는 문서만 우선 설명'하도록 완화한다.
    """
    tags, _kws = resolve_policy_boost_keywords(user_question)
    if not tags:
        return ""
    return (
        "\n        [답변 우선순위]\n"
        "        아래 질문군에 해당합니다. retrieved_documents에서 관련성이 높은 문서를 "
        "기존 출력 규칙을 유지한 채 먼저 안내하세요.\n"
        "        관련 문서가 일부만 있으면 있는 것만 설명하고, "
        "키워드 부재 안내 문구로 답변을 대체하지 마세요.\n"
        "        문서에 없는 내용은 작성하지 마세요.\n"
        f"        - 태그: {', '.join(sorted(tags))}\n"
    )
