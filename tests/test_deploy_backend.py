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


def test_cli_forwards_explicit_migration_permission(monkeypatch):
    import sys

    module = load_script()
    calls = []
    monkeypatch.setattr(sys, "argv", ["deploy_backend.py", "--socket", "/tmp/test-control", "--migrate"])
    monkeypatch.setattr(module, "run", lambda args, **kwargs: calls.append(args))
    monkeypatch.setattr(module, "package_release", lambda root, output: output.write_bytes(b"test archive"))
    module.main()
    remote = next(call[-1] for call in calls if call[0] == "ssh" and call[-1].startswith("bash "))
    assert remote.split()[-1] == "true"


@pytest.mark.parametrize(
    "permission,current,upgrade_fails,expected",
    [
        ("false", "5c8e9f0a12b3", False, 1),
        ("true", "5c8e9f0a12b3", False, 0),
        ("true", "5c8e9f0a12b3", True, 1),
        ("false", "e7a13c9b40d2", False, 0),
    ],
)
def test_remote_migration_gate(tmp_path, permission, current, upgrade_fails, expected):
    import os
    import subprocess

    source = (SCRIPT.parents[1] / "deploy/update-backend.sh").read_text()
    start = source.index("sudo -n docker compose run --rm --no-deps -T api alembic current >")
    end = source.index("switched=true", start)
    block = source[start:end]
    prefix = r"""
set -Eeuo pipefail
backup="$TEST_DIRECTORY"
migrate="$TEST_PERMISSION"
revision="$TEST_REVISION"
sudo() {
    case "$*" in
        *"alembic current")
            if [[ "$revision" == "e7a13c9b40d2" ]]; then printf '%s (head)\n' "$revision"
            else printf '%s\n' "$revision"; fi ;;
        *"alembic heads") printf 'e7a13c9b40d2 (head)\n' ;;
        *"alembic upgrade head")
            printf 'upgrade\n' >> "$backup/calls"
            [[ "$TEST_UPGRADE_FAILS" == "false" ]] || return 1
            revision=e7a13c9b40d2 ;;
        *) return 9 ;;
    esac
}
"""
    env = {
        **os.environ,
        "TEST_DIRECTORY": str(tmp_path),
        "TEST_PERMISSION": permission,
        "TEST_REVISION": current,
        "TEST_UPGRADE_FAILS": str(upgrade_fails).lower(),
    }
    result = subprocess.run(["bash", "-c", prefix + block], env=env, capture_output=True, text=True)
    assert result.returncode == expected, result.stderr + result.stdout
    upgraded = (tmp_path / "calls").exists()
    assert upgraded == (permission == "true" and current != "e7a13c9b40d2")
