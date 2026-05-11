"""Execute suites of evaluation cases."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from pathlib import Path

from .assertions import extract_path, get_matcher
from .loader import discover_suites, load_suite_file
from .runners import get as get_runner
from .schemas import (
    AssertionResult,
    Case,
    CaseResult,
    CaseRun,
    RunReport,
    SuiteResult,
)

logger = logging.getLogger(__name__)


def _evaluate_assertions(case: Case, output: dict) -> list[AssertionResult]:
    results: list[AssertionResult] = []
    for assertion in case.asserts:
        actual = extract_path(output, assertion.field)
        matcher = get_matcher(assertion.op)
        passed, detail = matcher(actual, assertion.value)
        results.append(AssertionResult(assertion=assertion, passed=passed, actual=actual, detail=detail))
    return results


async def _run_once(case: Case) -> CaseRun:
    runner = get_runner(case.runner)
    start = time.monotonic()
    try:
        output = await runner(case.inputs)
    except Exception as exc:  # noqa: BLE001
        elapsed = time.monotonic() - start
        logger.exception("[eval] runner failed: case=%s", case.name)
        return CaseRun(output={}, assertion_results=[], duration_seconds=elapsed, error=repr(exc))
    elapsed = time.monotonic() - start
    return CaseRun(
        output=output,
        assertion_results=_evaluate_assertions(case, output),
        duration_seconds=elapsed,
    )


async def run_case(case: Case) -> CaseResult:
    runs = [await _run_once(case) for _ in range(max(1, case.runs))]
    return CaseResult(case=case, runs=runs)


def _case_matches_tags(case: Case, tags_filter: list[str] | None) -> bool:
    if not tags_filter:
        return True
    return any(tag in case.tags for tag in tags_filter)


async def run_suite(
    suite_label: str,
    cases: Iterable[Case],
    *,
    tags: list[str] | None = None,
    name_filter: str | None = None,
) -> SuiteResult:
    selected: list[Case] = []
    for case in cases:
        if not _case_matches_tags(case, tags):
            continue
        if name_filter and name_filter not in case.name:
            continue
        selected.append(case)
    results = [await run_case(case) for case in selected]
    return SuiteResult(suite=suite_label, cases=results)


async def run_all(
    cases_root: Path,
    *,
    suite_filter: str | None = None,
    tags: list[str] | None = None,
    name_filter: str | None = None,
) -> RunReport:
    suite_results: list[SuiteResult] = []
    for yaml_path in discover_suites(cases_root):
        if suite_filter and suite_filter not in str(yaml_path.relative_to(cases_root)):
            continue
        suite_label, cases = load_suite_file(yaml_path)
        result = await run_suite(suite_label, cases, tags=tags, name_filter=name_filter)
        if result.cases:
            suite_results.append(result)
    return RunReport(suites=suite_results)


def run_all_sync(*args, **kwargs) -> RunReport:
    return asyncio.run(run_all(*args, **kwargs))
