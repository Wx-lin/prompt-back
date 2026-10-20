"""Run isolated profile tests with SQLAlchemy's bundled Python tracing fallback."""

import sys
from pathlib import Path

import coverage
import pytest


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.wechat_coverage import PureSqlAlchemy

    sys.meta_path.insert(0, PureSqlAlchemy())
    measurement = coverage.Coverage(
        branch=True, source=["app.services.user_profile"], data_file="/tmp/aigc-profile-coverage"
    )
    measurement.start()
    result = pytest.main(
        [
            str(Path(__file__).with_name("test_user_profile.py")),
            str(Path(__file__).with_name("test_profile_validation.py")),
            str(Path(__file__).with_name("test_profile_edges.py")),
            "-q",
            "--tb=short",
        ]
    )
    measurement.stop()
    measurement.save()
    percent = measurement.report(show_missing=True)
    return result or (1 if percent < 80 else 0)


if __name__ == "__main__":
    raise SystemExit(main())
