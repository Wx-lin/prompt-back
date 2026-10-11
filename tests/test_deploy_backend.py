import importlib.util
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "deploy_backend.py"


def load_script():
    spec = importlib.util.spec_from_file_location("deploy_backend", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_contains_required_source_but_no_secrets_or_cache(tmp_path):
    module = load_script()
    for directory in module.SOURCE_DIRS:
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "source.py").write_text("x = 1")
        (tmp_path / directory / ".env").write_text("secret")
        (tmp_path / directory / "__pycache__").mkdir()
        (tmp_path / directory / "__pycache__" / "x.pyc").write_bytes(b"cache")
    for file in module.SOURCE_FILES:
        (tmp_path / file).write_text("config")
    output = tmp_path / "release.tar.gz"
    module.package_release(tmp_path, output)
    with tarfile.open(output) as archive:
        names = archive.getnames()
    assert "backend/app/source.py" in names
    assert "backend/deploy/source.py" in names
    assert not any(".env" in name or "__pycache__" in name for name in names)


def test_missing_source_stops_before_creating_release(tmp_path):
    module = load_script()
    with pytest.raises(FileNotFoundError):
        module.package_release(tmp_path, tmp_path / "release.tar.gz")
