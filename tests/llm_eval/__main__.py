"""CLI entry: ``python -m tests.llm_eval [...flags]``."""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from . import runners  # noqa: F401 — populate runner registry
from .report import render_console, write_json
from .runner import run_all


def _build_argparser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m tests.llm_eval")
    parser.add_argument(
        "--cases-dir",
        type=Path,
        default=Path(__file__).parent / "cases",
        help="Root directory of YAML suites.",
    )
    parser.add_argument(
        "--suite",
        default=None,
        help="Substring filter on the suite YAML path (e.g. 'next_intent' or 'guards').",
    )
    parser.add_argument(
        "--tag",
        action="append",
        default=None,
        help="Run only cases with the given tag. Repeatable; OR-combined.",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Substring filter on case names.",
    )
    parser.add_argument(
        "--json",
        type=Path,
        default=None,
        help="Write a machine-readable JSON report to this path.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_argparser().parse_args(argv)
    report = asyncio.run(
        run_all(
            args.cases_dir,
            suite_filter=args.suite,
            tags=args.tag,
            name_filter=args.name,
        )
    )
    print(render_console(report))
    if args.json:
        write_json(report, args.json)
        print(f"\nJSON report written to {args.json}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
