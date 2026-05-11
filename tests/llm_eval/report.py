"""Console + JSON reports for harness runs."""
from __future__ import annotations

import json
from pathlib import Path

from .schemas import CaseResult, RunReport, SuiteResult


def _format_case_line(case: CaseResult) -> str:
    status = "PASS" if case.passed else "FAIL"
    rate = f"{case.pass_count}/{len(case.runs)}"
    duration_ms = case.mean_duration_seconds * 1000
    return f"  [{status}] {case.case.name}  ({rate} runs, {duration_ms:.0f}ms avg)"


def _format_failure_detail(case: CaseResult) -> list[str]:
    out: list[str] = []
    if case.passed:
        return out
    out.append(f"  - case: {case.case.name}")
    if case.case.notes:
        out.append(f"    notes: {case.case.notes}")
    for run_idx, run in enumerate(case.runs, 1):
        if run.passed:
            continue
        out.append(f"    run #{run_idx} (took {run.duration_seconds*1000:.0f}ms):")
        if run.error:
            out.append(f"      ERROR: {run.error}")
            continue
        for ar in run.assertion_results:
            if ar.passed:
                continue
            out.append(
                f"      [FAIL] op={ar.assertion.op} field={ar.assertion.field} "
                f"value={ar.assertion.value!r} -> {ar.detail}"
            )
        # Show the actual output to make root-causing easy.
        out.append(f"      output: {json.dumps(run.output, ensure_ascii=False, default=str)[:400]}")
    return out


def render_console(report: RunReport) -> str:
    lines: list[str] = []
    for suite in report.suites:
        lines.append(f"\n[{suite.suite}]  runner cases: {len(suite.cases)}, total runs: {suite.total_runs}")
        for case in suite.cases:
            lines.append(_format_case_line(case))

    fail_lines: list[str] = []
    for suite in report.suites:
        for case in suite.cases:
            fail_lines.extend(_format_failure_detail(case))
    if fail_lines:
        lines.append("\n--- Failures ---")
        lines.extend(fail_lines)

    total_cases = sum(len(s.cases) for s in report.suites)
    total_passed = sum(s.pass_count for s in report.suites)
    total_runs = sum(s.total_runs for s in report.suites)
    overall = "PASS" if report.passed else "FAIL"
    lines.append(
        f"\n=== {overall}: {total_passed}/{total_cases} cases, {total_runs} runs ==="
    )
    return "\n".join(lines)


def _case_to_dict(case: CaseResult) -> dict:
    return {
        "name": case.case.name,
        "runner": case.case.runner,
        "tags": case.case.tags,
        "runs": len(case.runs),
        "pass_count": case.pass_count,
        "pass_rate": case.pass_rate,
        "passed": case.passed,
        "mean_duration_seconds": case.mean_duration_seconds,
        "failures": [
            {
                "run_index": idx,
                "error": r.error,
                "failed_assertions": [
                    {
                        "op": ar.assertion.op,
                        "field": ar.assertion.field,
                        "expected": ar.assertion.value,
                        "actual": ar.actual,
                        "detail": ar.detail,
                    }
                    for ar in r.assertion_results
                    if not ar.passed
                ],
                "output": r.output,
            }
            for idx, r in enumerate(case.runs, 1)
            if not r.passed
        ],
    }


def render_json(report: RunReport) -> str:
    payload = {
        "passed": report.passed,
        "suites": [
            {
                "suite": s.suite,
                "passed": s.passed,
                "cases": [_case_to_dict(c) for c in s.cases],
            }
            for s in report.suites
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


def write_json(report: RunReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_json(report), encoding="utf-8")


def _suite_iter_results(report: RunReport):
    for suite in report.suites:
        yield suite, suite.cases


__all__ = ["render_console", "render_json", "write_json", "SuiteResult"]
