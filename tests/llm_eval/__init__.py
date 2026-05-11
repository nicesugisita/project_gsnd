"""LLM classifier/prompt regression harness.

Suites are YAML files under ``cases/<runner>/<suite>.yaml`` and are executed by
the runner registered under that name. Each case is asserted with a small set
of matchers and the result of N independent runs is reported as a pass rate.
"""

from .schemas import Assertion, Case, CaseResult, RunReport, SuiteResult

__all__ = ["Assertion", "Case", "CaseResult", "RunReport", "SuiteResult"]
