"""Measure the private image analysis feature with real isolated test services.

Run from the repository root after setting AIGC_TEST_DATABASE_URL and
AIGC_TEST_REDIS_URL to disposable PostgreSQL and Redis test services::

    uv run --with coverage python tests/prompt_analysis_coverage.py

SQLAlchemy's bundled Python modules are selected only in this test process.
Tracing is suspended only during asyncpg's synchronous Cython Protocol
constructor, which otherwise crashes when these dependencies run under tracing.
This workaround never excludes or pauses feature business code. Greenlet and thread tracing cover
SQLAlchemy's async bridge and storage/image work dispatched with to_thread.
Reports are saved under the existing gitignored htmlcov/ directory.
"""

import json
import os
import platform
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import coverage
import pytest

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIRECTORY = ROOT / "htmlcov" / "prompt-analysis"
SOURCES = [
    "app.services.wechat_auth",
    "app.utils.jwt",
    "app.models.wechat_identity",
    "app.routers.auth",
    "app.services.vision",
    "app.services.prompt_analysis_images",
    "app.models.prompt_analysis",
    "app.schemas.prompt_analysis",
    "app.routers.prompt_analyses",
    "app.services.prompt_analysis",
    "app.utils.prompt_analysis_http",
    "app.workers.prompt_analysis",
]


@contextmanager
def compatible_dependency_tracing():
    """Patch third-party imports and one Cython constructor in this process."""
    import asyncpg.connect_utils as connect_utils

    from tests.wechat_coverage import PureSqlAlchemy

    finder = PureSqlAlchemy()
    original_namespace = connect_utils.protocol
    original_protocol = original_namespace.Protocol

    def protocol_without_trace(*args, **kwargs):
        previous_trace = sys.gettrace()
        sys.settrace(None)
        try:
            return original_protocol(*args, **kwargs)
        finally:
            sys.settrace(previous_trace)

    sys.meta_path.insert(0, finder)
    connect_utils.protocol = SimpleNamespace(Protocol=protocol_without_trace)
    try:
        yield
    finally:
        connect_utils.protocol = original_namespace
        sys.meta_path.remove(finder)


def percentages(summary):
    lines = summary["num_statements"]
    branches = summary["num_branches"]
    return (
        100 * summary["covered_lines"] / lines if lines else 100.0,
        100 * summary["covered_branches"] / branches if branches else 100.0,
    )


def report_percentages(report):
    print("\nExplicit line and branch percentages:", flush=True)
    for name, measured in report["files"].items():
        lines, branches = percentages(measured["summary"])
        print(f"{name}: lines={lines:.2f}% branches={branches:.2f}%", flush=True)
    summary = report["totals"]
    lines, branches = percentages(summary)
    print(
        f"TOTAL lines={lines:.2f}% "
        f"({summary['covered_lines']}/{summary['num_statements']}), "
        f"branches={branches:.2f}% "
        f"({summary['covered_branches']}/{summary['num_branches']})",
        flush=True,
    )
    return lines, branches


def main():
    if not all(os.getenv(key) for key in ("AIGC_TEST_DATABASE_URL", "AIGC_TEST_REDIS_URL")):
        print("Set explicit isolated PostgreSQL and Redis test URLs before running coverage.")
        return 2
    sys.path.insert(0, str(ROOT))
    REPORT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    measurement = coverage.Coverage(
        branch=True,
        concurrency=["greenlet", "thread"],
        data_file=str(REPORT_DIRECTORY / ".coverage"),
        source=SOURCES,
    )
    tests = sorted((ROOT / "tests").glob("test_prompt_*.py")) + [
        ROOT / "tests" / filename
        for filename in ("test_image_prompt_auth.py", "test_wechat_auth.py", "test_jwt.py")
    ]
    print(f"Runtime: Python {platform.python_version()}, coverage {coverage.__version__}", flush=True)
    with compatible_dependency_tracing():
        measurement.start()
        try:
            result = pytest.main(["-q", *(str(path) for path in tests)])
        finally:
            measurement.stop()
            measurement.save()
    measurement.report(show_missing=True)
    report_path = REPORT_DIRECTORY / "coverage.json"
    measurement.json_report(outfile=str(report_path))
    lines, branches = report_percentages(json.loads(report_path.read_text()))
    return int(result) or int(lines < 80 or branches < 80)


if __name__ == "__main__":
    raise SystemExit(main())
