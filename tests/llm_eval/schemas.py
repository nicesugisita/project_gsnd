"""Dataclass models for the LLM evaluation harness."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Assertion:
    """A single matcher against the runner's output dictionary.

    op           — matcher name registered in :mod:`assertions`.
    field        — dotted path into the output dict (e.g. ``intent`` or ``preprocess.intent``).
    value        — operand for the matcher; semantics depend on ``op``.
    description  — optional human-readable note printed when the matcher fails.
    """
    op: str
    field: str
    value: Any = None
    description: str = ""


@dataclass(frozen=True)
class Case:
    """One evaluation case.

    name      — unique within its suite; appears in reports.
    runner    — registered runner key (e.g. ``next_intent``).
    tags      — free-form labels for filtering on the CLI.
    inputs    — kwargs forwarded to the runner adapter.
    asserts   — list of matchers; all must pass for the case to PASS.
    runs      — how many independent LLM calls per case (default 1).
    pass_threshold — fraction of runs that must pass for the case to be green (default 1.0).
    notes     — optional explanation shown in the failure block.
    """
    name: str
    runner: str
    inputs: dict[str, Any]
    asserts: list[Assertion]
    tags: list[str] = field(default_factory=list)
    runs: int = 1
    pass_threshold: float = 1.0
    notes: str = ""


@dataclass
class AssertionResult:
    assertion: Assertion
    passed: bool
    actual: Any
    detail: str = ""


@dataclass
class CaseRun:
    """Outcome of a single LLM invocation for one case."""
    output: dict[str, Any]
    assertion_results: list[AssertionResult]
    duration_seconds: float
    error: str | None = None

    @property
    def passed(self) -> bool:
        if self.error is not None:
            return False
        return all(r.passed for r in self.assertion_results)


@dataclass
class CaseResult:
    case: Case
    runs: list[CaseRun]

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.runs if r.passed)

    @property
    def pass_rate(self) -> float:
        return self.pass_count / len(self.runs) if self.runs else 0.0

    @property
    def passed(self) -> bool:
        return self.pass_rate >= self.case.pass_threshold

    @property
    def mean_duration_seconds(self) -> float:
        if not self.runs:
            return 0.0
        return sum(r.duration_seconds for r in self.runs) / len(self.runs)


@dataclass
class SuiteResult:
    suite: str
    cases: list[CaseResult]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.cases)

    @property
    def pass_count(self) -> int:
        return sum(1 for c in self.cases if c.passed)

    @property
    def total_runs(self) -> int:
        return sum(len(c.runs) for c in self.cases)


@dataclass
class RunReport:
    suites: list[SuiteResult]

    @property
    def passed(self) -> bool:
        return all(s.passed for s in self.suites)
