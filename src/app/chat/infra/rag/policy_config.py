"""정책 우선순위 YAML 로더 (P4 산출물).

`config/policy_rules.yaml` 를 읽어 priority tag 별 정적 규칙(anchor_keywords 등)을
제공한다. 운영자가 파일을 수정하면 mtime 변화 감지로 자동 리로드된다.

설계 원칙
- **하드코딩 제거**: 코드에 박혀 있던 anchor 키워드를 YAML 로 이동.
- **안전 폴백**: 파일이 없거나 로딩 실패 시 빈 매핑으로 동작 (기존 동작 유지).
- **저비용 캐시**: mtime 변화 시에만 디스크 재읽기. 한 번 읽으면 in-memory 상수.
- **YAGNI**: 현재 anchor_keywords 만 외부화. 추후 boost_weight·exclude 등이
  필요해지면 본 모듈에서 확장.

기존 DB 기반 키워드 캐시(`policy_priority._load_policy_keywords_from_db`)와는
역할이 다르다:
- DB: 정책팀이 수시로 변동하는 운영 키워드.
- YAML: 코드 동작에 가까운 정적·구조적 규칙.
"""
from __future__ import annotations

from collections.abc import Mapping
import logging
from pathlib import Path
import threading

import yaml

from app.core.config import Config

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 내부 캐시
# ---------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_CACHED_RULES: dict[str, dict] = {}
_CACHED_PATH: Path | None = None
_CACHED_MTIME: float = -1.0


def _resolve_path() -> Path:
    raw = (Config.POLICY_RULES_PATH or "").strip()
    if not raw:
        # 빈 설정이면 기본 경로 사용
        raw = "config/policy_rules.yaml"
    p = Path(raw)
    if not p.is_absolute():
        # 프로젝트 루트(`pyproject.toml` 기준)에서 상대 해석
        p = Path.cwd() / p
    return p


def _read_yaml(path: Path) -> dict[str, dict]:
    if not path.is_file():
        logger.info("[PolicyRules] YAML 미존재: %s — 빈 규칙으로 동작", path)
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except (yaml.YAMLError, OSError) as e:
        logger.warning("[PolicyRules] YAML 로드 실패 %s: %s — 빈 규칙으로 폴백", path, e)
        return {}
    if not isinstance(raw, dict):
        logger.warning("[PolicyRules] YAML 루트가 dict 아님(%s) — 빈 규칙으로 폴백", type(raw).__name__)
        return {}
    tags = raw.get("priority_tags")
    if not isinstance(tags, dict):
        return {}
    # 각 태그의 값은 dict 여야 함
    normalized: dict[str, dict] = {}
    for tag, spec in tags.items():
        if not isinstance(tag, str):
            continue
        if not isinstance(spec, dict):
            logger.warning("[PolicyRules] tag=%r 값이 dict 아님 — 무시", tag)
            continue
        normalized[tag] = spec
    return normalized


def _get_rules_with_refresh() -> Mapping[str, dict]:
    """파일 mtime 변화 시에만 디스크 재읽기."""
    global _CACHED_RULES, _CACHED_PATH, _CACHED_MTIME

    path = _resolve_path()
    try:
        current_mtime = path.stat().st_mtime if path.is_file() else -1.0
    except OSError:
        current_mtime = -1.0

    if _CACHED_PATH == path and _CACHED_MTIME == current_mtime:
        return _CACHED_RULES

    with _CACHE_LOCK:
        # 더블체크: 다른 스레드가 이미 갱신했는지 확인
        if _CACHED_PATH == path and _CACHED_MTIME == current_mtime:
            return _CACHED_RULES
        rules = _read_yaml(path)
        _CACHED_RULES = rules
        _CACHED_PATH = path
        _CACHED_MTIME = current_mtime
        logger.info(
            "[PolicyRules] cache refreshed: tags=%d source=%s",
            len(rules), path,
        )
    return _CACHED_RULES


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_anchor_keywords(tag: str) -> tuple[str, ...]:
    """특정 priority tag 의 anchor 키워드 튜플.

    문서 재정렬 시 1티어 부스트(=가장 위로 끌어올림)에 사용된다.
    YAML 누락·로드 실패 시 빈 튜플.
    """
    if not tag:
        return ()
    rules = _get_rules_with_refresh()
    spec = rules.get(tag)
    if not isinstance(spec, dict):
        return ()
    raw = spec.get("anchor_keywords")
    if not isinstance(raw, list):
        return ()
    # 문자열만, 빈값 제거, 원 순서 보존
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if not s or s in seen:
            continue
        cleaned.append(s)
        seen.add(s)
    return tuple(cleaned)


def get_anchor_keywords_casefold(tag: str) -> frozenset[str]:
    """anchor 키워드를 casefold 한 frozenset.

    `policy_priority.apply_policy_priority` 의 내부 비교용 — 본 함수가
    이전에 코드에 박혀 있던 `frozenset({"기초연금".casefold(), ...})` 를 대체한다.
    """
    return frozenset(kw.casefold() for kw in get_anchor_keywords(tag))


def reset_cache() -> None:
    """테스트·런타임 hot-reload 트리거 용도."""
    global _CACHED_RULES, _CACHED_PATH, _CACHED_MTIME
    with _CACHE_LOCK:
        _CACHED_RULES = {}
        _CACHED_PATH = None
        _CACHED_MTIME = -1.0
