"""Pytest entry that surfaces each YAML case as an individual test.

Marked ``llm_eval`` and ``slow`` so the default ``pytest`` run skips them — they
hit a real LLM. Run explicitly with::

    pytest -m llm_eval
    pytest tests/test_llm_eval.py -k guard_c
"""
from __future__ import annotations

import asyncio

import pytest

from tests.llm_eval import runners  # noqa: F401 — register runners
from tests.llm_eval.loader import discover_suites, load_suite_file
from tests.llm_eval.runner import run_case

CASES_ROOT = (__import__("pathlib").Path(__file__).parent / "llm_eval" / "cases")


def _collect():
    items = []
    for path in discover_suites(CASES_ROOT):
        suite_label, cases = load_suite_file(path)
        for case in cases:
            items.append(
                pytest.param(case, id=f"{suite_label}::{case.name}"),
            )
    return items


@pytest.mark.llm_eval
@pytest.mark.slow
@pytest.mark.parametrize("case", _collect())
def test_llm_eval_case(case) -> None:
    result = asyncio.run(run_case(case))
    if result.passed:
        return
    failures: list[str] = []
    for idx, run in enumerate(result.runs, 1):
        if run.passed:
            continue
        if run.error:
            failures.append(f"run #{idx}: ERROR {run.error}")
            continue
        for ar in run.assertion_results:
            if ar.passed:
                continue
            failures.append(
                f"run #{idx}: [{ar.assertion.op} {ar.assertion.field}] "
                f"expected={ar.assertion.value!r} actual={ar.actual!r} :: {ar.detail}"
            )
        failures.append(f"run #{idx} output: {run.output}")
    pytest.fail("\n".join(failures))
