"""Load suites of cases from YAML files.

Each suite file lives at ``cases/<runner>/<suite>.yaml`` and has the shape::

    suite: guards          # display name
    runner: next_intent    # which runner to invoke (defaults to parent directory)
    defaults:              # merged into every case's `inputs`
      is_clarification_question: false
    cases:
      - name: ...
        tags: [guard_a]
        runs: 1
        pass_threshold: 1.0
        inputs:
          messages: [...]
          user_query: ...
        asserts:
          - { op: equals, field: intent, value: MORE_INFO }
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .schemas import Assertion, Case


def _build_assertion(raw: dict[str, Any]) -> Assertion:
    missing = {"op", "field"} - raw.keys()
    if missing:
        raise ValueError(f"assertion missing keys {sorted(missing)}: {raw!r}")
    return Assertion(
        op=str(raw["op"]),
        field=str(raw["field"]),
        value=raw.get("value"),
        description=str(raw.get("description", "")),
    )


def _build_case(raw: dict[str, Any], default_runner: str, defaults: dict[str, Any]) -> Case:
    if "name" not in raw:
        raise ValueError(f"case missing 'name': {raw!r}")
    runner = str(raw.get("runner", default_runner))
    if not runner:
        raise ValueError(f"case {raw['name']!r} has no runner and no default runner")

    inputs = dict(defaults)
    inputs.update(raw.get("inputs") or {})

    asserts_raw = raw.get("asserts") or []
    if not isinstance(asserts_raw, list):
        raise ValueError(f"case {raw['name']!r} asserts must be a list")
    asserts = [_build_assertion(a) for a in asserts_raw]

    return Case(
        name=str(raw["name"]),
        runner=runner,
        inputs=inputs,
        asserts=asserts,
        tags=[str(t) for t in (raw.get("tags") or [])],
        runs=int(raw.get("runs", 1)),
        pass_threshold=float(raw.get("pass_threshold", 1.0)),
        notes=str(raw.get("notes", "")),
    )


def load_suite_file(path: Path) -> tuple[str, list[Case]]:
    """Return ``(suite_label, cases)`` for a YAML file."""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level must be a mapping")

    suite_label = str(data.get("suite") or path.stem)
    default_runner = str(data.get("runner") or path.parent.name)
    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ValueError(f"{path}: 'defaults' must be a mapping")

    cases_raw = data.get("cases") or []
    if not isinstance(cases_raw, list):
        raise ValueError(f"{path}: 'cases' must be a list")

    cases = [_build_case(c, default_runner, defaults) for c in cases_raw]
    return suite_label, cases


def discover_suites(root: Path) -> list[Path]:
    """All ``*.yaml`` files under ``cases/``. Sorted for deterministic order."""
    return sorted(p for p in root.rglob("*.yaml") if p.is_file())
