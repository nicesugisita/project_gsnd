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

# 정책 태그 강제 무력화용 변별 키워드(질환·신체부위·기능 등).
# 질문에 부분일치하면 LLM이 부여한 태그를 None으로 되돌린다.
_EXCLUDES_TABLE_DEFAULT = "gsnd_policy_priority_excludes"
_EXCLUDE_KEYWORDS_CACHE: Dict[str, Tuple[str, ...]] = {}
_EXCLUDE_KEYWORDS_CACHE_AT = 0.0
_EXCLUDE_KEYWORDS_LAST_CHECK_AT = 0.0
_EXCLUDE_KEYWORDS_LAST_VERSION: str = ""
_EXCLUDE_KEYWORDS_CACHE_READY = False


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


def _excludes_table_name_or_none() -> str | None:
    """exclude 테이블명. POLICY_PRIORITY_TABLE 옆에 _excludes 접미사로 추정.

    Config에 별도 키가 없으므로 기본 테이블명을 쓰되, 보안상 식별자 검증을 수행한다.
    """
    raw = _EXCLUDES_TABLE_DEFAULT
    if not _SAFE_IDENT_RE.fullmatch(raw):
        return None
    return raw


def _load_policy_excludes_from_db() -> Dict[str, Tuple[str, ...]]:
    """DB에서 정책 태그별 무력화 키워드를 읽어온다."""
    if not Config.POLICY_PRIORITY_DB_ENABLED:
        return {}

    table = _excludes_table_name_or_none()
    if not table:
        return {}

    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute(
            f"""
            SELECT policy_tag, keyword
            FROM {table}
            WHERE is_active = 1
            ORDER BY policy_tag ASC, keyword ASC
            """
        )
        rows = cur.fetchall() or []
    except Exception as e:
        logger.warning("[PolicyExclude] db load failed: %s", e)
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
        tag = str(row.get("policy_tag", "") or "").strip().lower().replace("-", "_")
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


def _load_policy_excludes_version_from_db() -> str:
    """exclude 테이블의 최신 버전(MAX(updated_at))."""
    if not Config.POLICY_PRIORITY_DB_ENABLED:
        return ""
    table = _excludes_table_name_or_none()
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
        return str(row[0] or "").strip() if row else ""
    except Exception as e:
        logger.warning("[PolicyExclude] version check failed: %s", e)
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


def _get_exclude_keywords_map() -> Dict[str, Tuple[str, ...]]:
    """태그별 exclude 키워드 맵 (boost 캐시와 동일한 TTL·변경체크 규칙)."""
    ttl = max(int(Config.POLICY_PRIORITY_CACHE_TTL_SEC or 0), 0)
    check_interval = max(int(Config.POLICY_PRIORITY_CHANGE_CHECK_SEC or 0), 0)
    now = time.monotonic()
    with _CACHE_LOCK:
        global _EXCLUDE_KEYWORDS_CACHE, _EXCLUDE_KEYWORDS_CACHE_AT
        global _EXCLUDE_KEYWORDS_LAST_CHECK_AT, _EXCLUDE_KEYWORDS_LAST_VERSION
        global _EXCLUDE_KEYWORDS_CACHE_READY

        has_cache = _EXCLUDE_KEYWORDS_CACHE_READY
        if has_cache and check_interval > 0 and _EXCLUDE_KEYWORDS_LAST_CHECK_AT:
            if (now - _EXCLUDE_KEYWORDS_LAST_CHECK_AT) < check_interval:
                return _EXCLUDE_KEYWORDS_CACHE

        if not has_cache and ttl > 0 and _EXCLUDE_KEYWORDS_CACHE_AT and (now - _EXCLUDE_KEYWORDS_CACHE_AT) < ttl:
            return _EXCLUDE_KEYWORDS_CACHE

        if has_cache and check_interval > 0:
            current_version = _load_policy_excludes_version_from_db()
            _EXCLUDE_KEYWORDS_LAST_CHECK_AT = now
            if current_version and current_version == _EXCLUDE_KEYWORDS_LAST_VERSION:
                return _EXCLUDE_KEYWORDS_CACHE

        db_map = _load_policy_excludes_from_db()
        _EXCLUDE_KEYWORDS_CACHE = db_map
        _EXCLUDE_KEYWORDS_CACHE_AT = now
        _EXCLUDE_KEYWORDS_LAST_CHECK_AT = now
        _EXCLUDE_KEYWORDS_LAST_VERSION = _load_policy_excludes_version_from_db()
        _EXCLUDE_KEYWORDS_CACHE_READY = True
        logger.info(
            "[PolicyExclude] cache refreshed: tags=%d version=%s",
            len(_EXCLUDE_KEYWORDS_CACHE),
            _EXCLUDE_KEYWORDS_LAST_VERSION or "(empty)",
        )
        return _EXCLUDE_KEYWORDS_CACHE


def strip_tag_by_exclusions(query: str, tag: Any) -> str | None:
    """LLM이 부여한 policy_priority_tag을 DB 기반 변별 키워드로 사후 무력화.

    - query에 해당 태그의 exclude 키워드가 부분일치(대소문자 무시)하면 None 반환.
    - tag이 None이거나 매칭이 없으면 그대로 반환.
    """
    if tag is None:
        return None
    normalized = str(tag).strip().lower().replace("-", "_")
    if not normalized or normalized in ("null", "none"):
        return None
    if normalized not in POLICY_PRIORITY_TAGS:
        return normalized  # 알 수 없는 태그는 그대로 (상위에서 None 처리)

    q = (query or "").strip()
    if not q:
        return normalized
    q_cf = q.casefold()
    excludes = _get_exclude_keywords_map().get(normalized, ())
    for kw in excludes:
        k = (kw or "").strip()
        if not k:
            continue
        if k.casefold() in q_cf:
            logger.info(
                "[PolicyExclude] tag=%s 무력화: matched_keyword=%r in query=%r",
                normalized,
                k,
                q[:80],
            )
            return None
    return normalized


def preload_policy_priority_cache() -> None:
    """앱 시작 시 정책 키워드(boost+exclude) 캐시를 즉시 채운다."""
    try:
        _get_tag_keywords_map()
    except Exception as e:
        logger.warning("[PolicyBoost] preload failed: %s", e)
    try:
        _get_exclude_keywords_map()
    except Exception as e:
        logger.warning("[PolicyExclude] preload failed: %s", e)


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
            try:
                _get_exclude_keywords_map()
            except Exception as e:
                logger.warning("[PolicyExclude] refresh worker error: %s", e)
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


POLICY_PRIORITY_TAGS: FrozenSet[str] = frozenset({"implant", "low_income", "elderly_benefits"})


def _normalize_policy_priority_tags(raw: Any) -> FrozenSet[str]:
    if raw is None:
        return frozenset()
    if isinstance(raw, str):
        value = raw.strip().lower().replace("-", "_")
        return frozenset({value}) if value in POLICY_PRIORITY_TAGS else frozenset()
    if isinstance(raw, (set, frozenset, list, tuple)):
        normalized = {
            str(item).strip().lower().replace("-", "_")
            for item in raw
            if str(item or "").strip().lower().replace("-", "_") in POLICY_PRIORITY_TAGS
        }
        return frozenset(normalized)
    return frozenset()


def resolve_policy_boost_keywords(policy_priority_tag: Any) -> Tuple[FrozenSet[str], Tuple[str, ...]]:
    """전처리에서 확정된 정책 태그와, 문서 매칭용 부스트 키워드 튜플을 반환한다."""
    tags = _normalize_policy_priority_tags(policy_priority_tag)
    if not tags:
        return frozenset(), ()
    return tags, _resolve_keywords_for_tags(tags)


def augment_okms_dual_query(
    policy_priority_tag: Any,
    vector_q: str,
    keyword_q: str,
) -> Tuple[str, str]:
    """Mariner Group A 검색 직전 (vector, keyword) 보강.

    트리플이 비어 있던 행을 건드리며 keyword 레그만 채우면, 수집 루프의 tri_built
    인덱스와 맞지 않아 키워드 결과가 버려질 수 있으므로, keyword 보강은 기존
    트리플 문자열이 있을 때만 한다. 빈 트리플 레그 보강은 `policy_extra_okms_searches`.
    """
    tags, kws = resolve_policy_boost_keywords(policy_priority_tag)
    vec = (vector_q or "").strip()
    kw = (keyword_q or "").strip()
    if not tags:
        return vec, kw

    # 하드코딩 보강어를 쓰지 않고 DB 로딩 키워드만 사용한다.
    if not kws:
        return vec, kw

    low_vec = vec.casefold()
    for needle in _pick_distinct_keywords(kws, n=2):
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


def policy_extra_okms_searches(policy_priority_tag: Any, reformed_query: str) -> List[Tuple[str, str]]:
    """정책 태그별 OKMS Group A 추가 검색 (vector, keyword) 쌍 — 빈 트리플·약한 검색 보강."""
    tags, _ = resolve_policy_boost_keywords(policy_priority_tag)
    rq = (reformed_query or "").strip()
    tag_keywords = _get_tag_keywords_map()
    if not tags or not rq:
        return []

    if "elderly_benefits" in tags:
        elderly_kws = tag_keywords.get("elderly_benefits", ())
        anchors = _pick_distinct_keywords(elderly_kws, n=2)
        if not anchors:
            return []
        pairs: List[Tuple[str, str]] = []
        for idx, anchor in enumerate(anchors):
            suffix = " 안내" if idx == 0 else ""
            pairs.append((f"{rq} {anchor}{suffix}".strip(), anchor))
        return pairs

    if "implant" in tags:
        implant_kws = _pick_distinct_keywords(tag_keywords.get("implant", ()), n=1)
        if not implant_kws:
            return []
        implant_kw = implant_kws[0]
        return [(f"{rq} {implant_kw} 지원", implant_kw)]

    if "low_income" in tags:
        low_income_kws = _pick_distinct_keywords(tag_keywords.get("low_income", ()), n=2)
        if not low_income_kws:
            return []
        joined = " ".join(low_income_kws)
        return [(f"{rq} {joined}".strip(), joined)]

    return []


def policy_supplement_welfare_queries(policy_priority_tag: Any) -> List[str]:
    """search 의도 WELFARE_CENTER/TEL 검색에 추가로 던질 짧은 쿼리."""
    tags, _ = resolve_policy_boost_keywords(policy_priority_tag)
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
    policy_priority_tag: Any,
    docs: List[Dict[str, Any]],
    *,
    log_prefix: str = "PolicyBoost",
    apply_enabled: bool = True,
) -> List[Dict[str, Any]]:
    """검색 후 문서 목록에 질문군별 soft-priority를 적용해 재정렬한다.

    - 문서 본문/제목에 정책 키워드가 포함된 후보를 앞으로 올린다.
    - 어느 문서에도 키워드가 없으면 원 순서를 유지한다(불필요한 순서 뒤집음 방지).
    - 동점 시 CHUNK_ID/ID 문자열로 안정 정렬.
    - apply_enabled=False: MORE_INFO 등 검색 순서 유지가 필요할 때 재정렬 생략.
    """
    if not docs:
        return docs
    if not apply_enabled:
        return docs

    tags, boost_keywords = resolve_policy_boost_keywords(policy_priority_tag)
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


def soft_priority_instruction_for_prompt(policy_priority_tag: Any) -> str:
    """최종 LLM user 메시지에 붙일 짧은 우선순위 안내(문서 근거만).

    주의:
    - 특정 키워드 목록을 그대로 노출하면 모델이 '키워드 부재' 안내로 치우칠 수 있어
      태그만 전달하고, '있는 문서만 우선 설명'하도록 완화한다.
    """
    tags, _kws = resolve_policy_boost_keywords(policy_priority_tag)
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
