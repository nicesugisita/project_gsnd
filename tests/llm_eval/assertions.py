"""Matchers used by the eval harness.

Each matcher receives ``(actual, operand)`` and returns ``(passed, detail)``.
``actual`` is the value extracted from the runner output via a dotted path.
``operand`` is the YAML-provided value for the matcher.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

Matcher = Callable[[Any, Any], tuple[bool, str]]


def _stringify(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def equals(actual: Any, operand: Any) -> tuple[bool, str]:
    ok = actual == operand
    return ok, "" if ok else f"expected {operand!r}, got {actual!r}"


def not_equals(actual: Any, operand: Any) -> tuple[bool, str]:
    ok = actual != operand
    return ok, "" if ok else f"expected != {operand!r}, got {actual!r}"


def intent_in(actual: Any, operand: Any) -> tuple[bool, str]:
    allowed = _as_list(operand)
    ok = actual in allowed
    return ok, "" if ok else f"expected one of {allowed!r}, got {actual!r}"


def intent_not_in(actual: Any, operand: Any) -> tuple[bool, str]:
    forbidden = _as_list(operand)
    ok = actual not in forbidden
    return ok, "" if ok else f"expected NOT in {forbidden!r}, got {actual!r}"


def contains(actual: Any, operand: Any) -> tuple[bool, str]:
    haystack = _stringify(actual)
    needle = _stringify(operand)
    ok = needle in haystack
    return ok, "" if ok else f"expected substring {needle!r} in {haystack!r}"


def not_contains(actual: Any, operand: Any) -> tuple[bool, str]:
    haystack = _stringify(actual)
    needle = _stringify(operand)
    ok = needle not in haystack
    return ok, "" if ok else f"expected substring {needle!r} NOT in {haystack!r}"


def contains_all(actual: Any, operand: Any) -> tuple[bool, str]:
    haystack = _stringify(actual)
    needles = [_stringify(v) for v in _as_list(operand)]
    missing = [n for n in needles if n not in haystack]
    return (not missing), "" if not missing else f"missing substrings {missing!r} in {haystack!r}"


def matches_regex(actual: Any, operand: Any) -> tuple[bool, str]:
    pattern = re.compile(_stringify(operand))
    haystack = _stringify(actual)
    ok = bool(pattern.search(haystack))
    return ok, "" if ok else f"regex {operand!r} did not match {haystack!r}"


def is_truthy(actual: Any, operand: Any) -> tuple[bool, str]:  # noqa: ARG001
    ok = bool(actual)
    return ok, "" if ok else "expected truthy"


def is_falsy(actual: Any, operand: Any) -> tuple[bool, str]:  # noqa: ARG001
    ok = not bool(actual)
    return ok, "" if ok else f"expected falsy, got {actual!r}"


def length_eq(actual: Any, operand: Any) -> tuple[bool, str]:
    expected = int(operand)
    seq = _as_list(actual) if not isinstance(actual, (list, tuple, str)) else actual
    got = len(seq)
    ok = got == expected
    return ok, "" if ok else f"expected length {expected}, got {got}"


def length_gte(actual: Any, operand: Any) -> tuple[bool, str]:
    threshold = int(operand)
    seq = _as_list(actual) if not isinstance(actual, (list, tuple, str)) else actual
    got = len(seq)
    ok = got >= threshold
    return ok, "" if ok else f"expected length >= {threshold}, got {got}"


def any_contains(actual: Iterable[Any], operand: Any) -> tuple[bool, str]:
    """At least one element of ``actual`` contains the operand as substring."""
    needle = _stringify(operand)
    items = _as_list(actual)
    ok = any(needle in _stringify(item) for item in items)
    return ok, "" if ok else f"no element contains {needle!r}; items={items!r}"


def all_contain(actual: Iterable[Any], operand: Any) -> tuple[bool, str]:
    """Every element of ``actual`` contains the operand as substring."""
    needle = _stringify(operand)
    items = _as_list(actual)
    missing = [i for i in items if needle not in _stringify(i)]
    return (not missing), "" if not missing else f"these elements missing {needle!r}: {missing!r}"


MATCHERS: dict[str, Matcher] = {
    "equals": equals,
    "not_equals": not_equals,
    "intent_in": intent_in,
    "intent_not_in": intent_not_in,
    "contains": contains,
    "not_contains": not_contains,
    "contains_all": contains_all,
    "matches_regex": matches_regex,
    "is_truthy": is_truthy,
    "is_falsy": is_falsy,
    "length_eq": length_eq,
    "length_gte": length_gte,
    "any_contains": any_contains,
    "all_contain": all_contain,
}


def get_matcher(op: str) -> Matcher:
    try:
        return MATCHERS[op]
    except KeyError as exc:
        raise ValueError(
            f"Unknown matcher {op!r}. Available: {sorted(MATCHERS)}"
        ) from exc


def extract_path(payload: dict, path: str) -> Any:
    """Resolve a dotted path. Missing keys return ``None`` rather than raising."""
    current: Any = payload
    for segment in path.split("."):
        if isinstance(current, dict):
            current = current.get(segment)
        else:
            return None
    return current
