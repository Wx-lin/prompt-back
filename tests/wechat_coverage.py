"""Reproducible coverage runner for the new WeChat service and identity model.

Python 3.13 tracing breaks the installed SQLAlchemy 2.1 Cython cache traversal.
Select SQLAlchemy's bundled equivalent Python modules in this test process;
normal pytest and application processes still use the installed C extensions.
"""

import importlib.abc
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import coverage
import pytest


class PureSqlAlchemy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if not fullname.startswith("sqlalchemy.") or not fullname.endswith("_cy"):
            return None
        compiled = importlib.machinery.PathFinder.find_spec(fullname, path)
        if compiled and compiled.origin:
            source = Path(compiled.origin.split(".cpython-")[0] + ".py")
            if source.is_file():
                return importlib.util.spec_from_file_location(fullname, source)
        return None


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.meta_path.insert(0, PureSqlAlchemy())
    measurement = coverage.Coverage(
        branch=True, source=["app.services.wechat_auth", "app.models.wechat_identity"]
    )
    measurement.start()
    result = pytest.main([str(Path(__file__).with_name("test_wechat_auth.py")), "-q"])
    measurement.stop()
    measurement.save()
    percent = measurement.report(show_missing=True)
    return result or (1 if percent < 80 else 0)


if __name__ == "__main__":
    raise SystemExit(main())
