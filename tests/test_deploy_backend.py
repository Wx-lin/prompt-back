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
    end = source.index("\nswitched=true", start)
    block = source[start:end]
    prefix = r"""
set -Eeuo pipefail
backup="$TEST_DIRECTORY"
migrate="$TEST_PERMISSION"
auto_migration=
reviewed_migration=
admin_hash=
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


@pytest.mark.parametrize(
    "current,head,allowed,expected",
    [
        ("5c8e9f0a12b3", "e7a13c9b40d2", "e7a13c9b40d2", 0),
        ("111111111111", "e7a13c9b40d2", "e7a13c9b40d2", 1),
        ("5c8e9f0a12b3", "ffffffffffff", "e7a13c9b40d2", 1),
        ("5c8e9f0a12b3", "e7a13c9b40d2", "", 1),
        ("e7a13c9b40d2", "f8b24d0c51e3", "e7a13c9b40d2", 1),
        ("c74d219e8a60", "d91a72f4b308", "e7a13c9b40d2", 1),
        ("5c8e9f0a12b3", "d91a72f4b308", "e7a13c9b40d2", 1),
        ("d91a72f4b308", "e04b75ad6312", "e7a13c9b40d2", 1),
        ("c74d219e8a60", "e04b75ad6312", "e7a13c9b40d2", 1),
    ],
)
def test_automatic_migration_only_for_reviewed_parent_head(tmp_path, current, head, allowed, expected):
    import os
    import subprocess

    source = (SCRIPT.parents[1] / "deploy/update-backend.sh").read_text()
    start = source.index("sudo -n docker compose run --rm --no-deps -T api alembic current >")
    block = source[start : source.index("\nswitched=true", start)]
    prefix = r"""
set -Eeuo pipefail
backup="$TEST_DIRECTORY"
migrate=false
auto_migration="$TEST_ALLOWED"
reviewed_migration=
admin_hash=
revision="$TEST_REVISION"
sudo() {
 case "$*" in
 *"alembic current") printf '%s\n' "$revision" ;;
 *"alembic heads") printf '%s (head)\n' "$TEST_HEAD" ;;
 *"alembic upgrade head") printf 'upgrade\n' > "$backup/calls"; revision="$TEST_HEAD" ;;
 *) return 9 ;;
 esac
}
"""
    env = {
        **os.environ,
        "TEST_DIRECTORY": str(tmp_path),
        "TEST_ALLOWED": allowed,
        "TEST_REVISION": current,
        "TEST_HEAD": head,
    }
    result = subprocess.run(["bash", "-c", prefix + block], env=env, capture_output=True, text=True)
    assert result.returncode == expected, result.stderr + result.stdout
    assert (tmp_path / "calls").exists() == (expected == 0)


def test_admin_checksum_rejected_before_server_mutation(tmp_path):
    import subprocess

    source = (SCRIPT.parents[1] / "deploy/update-backend.sh").read_text()
    start = source.index('if [[ -n "$admin_hash" ]]; then')
    end = source.index("sudo -n docker compose config --quiet", start)
    (tmp_path / "admin.tar.gz").write_bytes(b"wrong artifact")
    prefix = f'backup="{tmp_path}"; admin_hash={"0" * 64}; set -Eeuo pipefail\n'
    result = subprocess.run(["bash", "-c", prefix + source[start:end]], capture_output=True)
    assert result.returncode != 0


@pytest.mark.parametrize("fail", [False, True])
def test_admin_assets_published_before_index_and_rollback_restores_index(tmp_path, fail):
    import subprocess

    source = (SCRIPT.parents[1] / "deploy/update-backend.sh").read_text()
    finish = source[source.index("finish() {") : source.index("for attempt in {1..60}")]
    start = source.rindex('if [[ -n "$admin_hash" ]]; then')
    block = source[start : source.index("\nswitched=true", start)]
    dist = tmp_path / "backend/admin-panel/dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("old index")
    (dist / "assets/old.js").write_text("old JS")
    (dist / "favicon.svg").write_text("old favicon")
    backup = tmp_path / "backup"
    backup.mkdir()
    new = tmp_path / "new"
    (new / "assets").mkdir(parents=True)
    (new / "index.html").write_text("new index")
    (new / "assets/new.js").write_text("new JS")
    (new / "favicon.svg").write_text("new favicon")
    with tarfile.open(backup / "admin.tar.gz", "w:gz") as archive:
        archive.add(new / "index.html", arcname="index.html")
        archive.add(new / "assets", arcname="assets")
        archive.add(new / "favicon.svg", arcname="favicon.svg")
    prefix = (
        "set -Eeuo pipefail; umask 077; backup=backup; admin_hash=x; admin_switched=false; "
        "switched=false; success=false; old_id=; timer_was_active=false\n"
    )
    # Simulate root-owned bind-mount files: writes must pass the sudo boundary.
    prefix += r"""
ADMIN_WRITE_PRIVILEGED=no
guard_write() {
    case "${@: -1}" in
        backend/admin-panel/dist*) [[ "$ADMIN_WRITE_PRIVILEGED" == yes ]] || return 77 ;;
    esac
}
cp() { guard_write "$@" || return; command cp "$@"; }
install() { guard_write "$@" || return; command install "$@"; }
mv() { guard_write "$@" || return; command mv "$@"; }
mkdir() { guard_write "$@" || return; command mkdir "$@"; }
find() {
    [[ "$1" != backend/admin-panel/dist || "$ADMIN_WRITE_PRIVILEGED" == yes ]] || return 77
    command find "$@"
}
sudo() {
    [[ "$1" == -n ]] || return 78
    shift
    printf '%s\n' "$*" >> "$backup/privileged-writes"
    ADMIN_WRITE_PRIVILEGED=yes "$@"
}
"""
    suffix = "\nexit 1" if fail else "\nsuccess=true"
    result = subprocess.run(
        ["bash", "-c", prefix + finish + block + suffix], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == (1 if fail else 0), result.stderr
    assert (dist / "index.html").read_text() == ("old index" if fail else "new index")
    assert (dist / "assets/old.js").exists()
    assert (dist / "assets/new.js").exists()
    assert (dist / "favicon.svg").read_text() == "new favicon"
    writes = (backup / "privileged-writes").read_text()
    assert "install -m 0644" in writes
    assert "mv backend/admin-panel/dist/index.html.next" in writes
    if fail:
        assert "index.html.rollback" in writes
    assert (dist / "index.html").stat().st_mode & 0o777 == 0o644
    assert (dist / "assets/new.js").stat().st_mode & 0o777 == 0o644
    assert (dist / "assets").stat().st_mode & 0o777 == 0o755


def test_verified_admin_archive_rejects_traversal(tmp_path):
    import hashlib
    import io
    import subprocess

    source = (SCRIPT.parents[1] / "deploy/update-backend.sh").read_text()
    start = source.index('if [[ -n "$admin_hash" ]]; then')
    block = source[start : source.index("sudo -n docker compose config --quiet", start)]
    bundle = tmp_path / "admin.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        item = tarfile.TarInfo("../escape.html")
        item.size = 3
        archive.addfile(item, io.BytesIO(b"bad"))
    digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
    prefix = f'set -Eeuo pipefail; backup="{tmp_path}"; admin_hash={digest}\n'
    result = subprocess.run(["bash", "-c", prefix + block], capture_output=True, text=True)
    assert result.returncode != 0
    assert "Unsafe admin archive member" in result.stderr
    assert not (tmp_path.parent / "escape.html").exists()


@pytest.mark.parametrize("response,expected", [("new index", 0), ("old index", 1), ("", 1)])
def test_public_admin_health_requires_exact_new_index(tmp_path, response, expected):
    import subprocess

    source = (SCRIPT.parents[1] / "deploy/update-backend.sh").read_text()
    start = source.index("admin_healthy() {")
    function = source[start : source.index("\nhealthy=false", start)]
    (tmp_path / "admin.new").mkdir()
    (tmp_path / "admin.new/index.html").write_text("new index")
    (tmp_path / "response").write_text(response)
    prefix = f'backup="{tmp_path}"; admin_hash=x\n'
    prefix += r"""
curl() { cp "$backup/response" "${@: -1}"; }
"""
    result = subprocess.run(["bash", "-c", prefix + function + "\nadmin_healthy"], capture_output=True)
    assert result.returncode == expected


def test_release_lock_waits_for_concurrent_frontend_deploy_with_bound():
    source = (SCRIPT.parents[1] / "deploy/update-backend.sh").read_text()
    assert "exec 8> .release-deploy.lock" in source
    assert "flock -w 600 8" in source
    assert "flock -w 600 9" in source
    assert "flock -n 8" not in source


@pytest.mark.parametrize(
    "permission,current,head,expected,upgraded,reviewed",
    [
        ("true", "e7a13c9b40d2", "f8b24d0c51e3", 0, True, "f8b24d0c51e3"),
        ("true", "f8b24d0c51e3", "f8b24d0c51e3", 0, False, "f8b24d0c51e3"),
        ("true", "5c8e9f0a12b3", "f8b24d0c51e3", 1, False, "f8b24d0c51e3"),
        ("true", "e7a13c9b40d2", "ffffffffffff", 1, False, "f8b24d0c51e3"),
        ("false", "e7a13c9b40d2", "f8b24d0c51e3", 1, False, "f8b24d0c51e3"),
        ("true", "a3e6b18d4092", "c74d219e8a60", 0, True, "c74d219e8a60"),
        ("true", "c74d219e8a60", "c74d219e8a60", 0, False, "c74d219e8a60"),
        ("true", "9c35e1a62f4", "c74d219e8a60", 1, False, "c74d219e8a60"),
        ("false", "a3e6b18d4092", "c74d219e8a60", 1, False, "c74d219e8a60"),
        ("true", "9c35e1a62f4", "a3e6b18d4092", 0, True, "a3e6b18d4092"),
        ("true", "a3e6b18d4092", "a3e6b18d4092", 0, False, "a3e6b18d4092"),
        ("false", "9c35e1a62f4", "a3e6b18d4092", 1, False, "a3e6b18d4092"),
        ("true", "e7a13c9b40d2", "a3e6b18d4092", 1, False, "a3e6b18d4092"),
        ("true", "c74d219e8a60", "d91a72f4b308", 0, True, "d91a72f4b308"),
        ("true", "d91a72f4b308", "d91a72f4b308", 0, False, "d91a72f4b308"),
        ("true", "a3e6b18d4092", "d91a72f4b308", 1, False, "d91a72f4b308"),
        ("true", "ffffffffffff", "d91a72f4b308", 1, False, "d91a72f4b308"),
        ("true", "c74d219e8a60", "ffffffffffff", 1, False, "d91a72f4b308"),
        ("true", "c74d219e8a60", "c74d219e8a60", 1, False, "d91a72f4b308"),
        ("false", "c74d219e8a60", "d91a72f4b308", 1, False, "d91a72f4b308"),
        ("true", "d91a72f4b308", "e04b75ad6312", 0, True, "e04b75ad6312"),
        ("true", "e04b75ad6312", "e04b75ad6312", 0, False, "e04b75ad6312"),
        ("true", "c74d219e8a60", "e04b75ad6312", 1, False, "e04b75ad6312"),
        ("true", "ffffffffffff", "e04b75ad6312", 1, False, "e04b75ad6312"),
        ("true", "d91a72f4b308", "ffffffffffff", 1, False, "e04b75ad6312"),
        ("true", "d91a72f4b308", "d91a72f4b308", 1, False, "e04b75ad6312"),
        ("false", "d91a72f4b308", "e04b75ad6312", 1, False, "e04b75ad6312"),
        ("true", "d91a72f4b308", "e04b75ad6312", 1, False, "d91a72f4b308"),
    ],
)
def test_dispatch_migration_is_bounded(tmp_path, permission, current, head, expected, upgraded, reviewed):
    import os
    import subprocess

    source = (SCRIPT.parents[1] / "deploy/update-backend.sh").read_text()
    start = source.index("sudo -n docker compose run --rm --no-deps -T api alembic current >")
    block = source[start : source.index("\nswitched=true", start)]
    prefix = r"""
set -Eeuo pipefail
backup="$TEST_DIRECTORY"
migrate="$TEST_PERMISSION"
auto_migration=e7a13c9b40d2
reviewed_migration="$TEST_REVIEWED"
admin_hash=
revision="$TEST_REVISION"
sudo() {
 case "$*" in
 *"alembic current") printf '%s\n' "$revision" ;;
 *"alembic heads") printf '%s (head)\n' "$TEST_HEAD" ;;
 *"alembic upgrade head") printf 'upgrade\n' > "$backup/calls"; revision="$TEST_HEAD" ;;
 *) return 9 ;;
 esac
}
"""
    env = {
        **os.environ,
        "TEST_DIRECTORY": str(tmp_path),
        "TEST_PERMISSION": permission,
        "TEST_REVIEWED": reviewed,
        "TEST_REVISION": current,
        "TEST_HEAD": head,
    }
    result = subprocess.run(["bash", "-c", prefix + block], env=env, capture_output=True, text=True)
    assert result.returncode == expected, result.stderr + result.stdout
    assert (tmp_path / "calls").exists() == upgraded


@pytest.mark.parametrize(
    "event,target,expected",
    [
        ("push", "f8b24d0c51e3", "false:"),
        ("push", "a3e6b18d4092", "false:"),
        ("push", "c74d219e8a60", "false:"),
        ("push", "d91a72f4b308", "false:"),
        ("push", "e04b75ad6312", "false:"),
        ("pull_request", "d91a72f4b308", "false:"),
        ("pull_request", "e04b75ad6312", "false:"),
        ("workflow_dispatch", "e04b75ad6312", "true:e04b75ad6312"),
        ("workflow_dispatch", "d91a72f4b308", "true:d91a72f4b308"),
        ("workflow_dispatch", "c74d219e8a60", "true:c74d219e8a60"),
        ("workflow_dispatch", "a3e6b18d4092", "true:a3e6b18d4092"),
        ("workflow_dispatch", "none", "false:"),
        ("workflow_dispatch", "f8b24d0c51e3", "true:f8b24d0c51e3"),
        ("workflow_dispatch", "ffffffffffff", "false:"),
    ],
)
def test_workflow_only_explicit_dispatch_enables_migration(event, target, expected):
    import os
    import subprocess
    import textwrap

    source = (SCRIPT.parents[1] / ".github/workflows/deploy.yml").read_text()
    start = source.index("          migrate=false")
    end = source.index("          # Push deployments", start)
    block = textwrap.dedent(source[start:end])
    result = subprocess.run(
        ["bash", "-c", block + '\nprintf "%s:%s" "$migrate" "$reviewed_target"'],
        env={**os.environ, "DEPLOY_EVENT": event, "REVIEWED_MIGRATION": target},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == expected


def test_wechat_reviewed_migration_is_available_in_manual_workflow_choices():
    source = (SCRIPT.parents[1] / ".github/workflows/deploy.yml").read_text()
    choices = source.split("        options:\n", 1)[1].split("\npermissions:", 1)[0]
    assert "          - d91a72f4b308\n" in choices + "\n"


def test_image_prompt_reviewed_migration_is_available_in_manual_workflow_choices():
    source = (SCRIPT.parents[1] / ".github/workflows/deploy.yml").read_text()
    choices = source.split("        options:\n", 1)[1].split("\npermissions:", 1)[0]
    assert "          - e04b75ad6312\n" in choices + "\n"


@pytest.mark.parametrize("reviewed,expected", [("d91a72f4b308", 0), ("e04b75ad6312", 0), ("ffffffffffff", 1)])
def test_wechat_reviewed_argument_checked_before_server_access(reviewed, expected):
    import subprocess

    source = (SCRIPT.parents[1] / "deploy/update-backend.sh").read_text()
    validation = source[: source.index("cd /opt/aigc-prompt")]
    result = subprocess.run(
        [
            "bash",
            "-c",
            validation,
            "fixture",
            "backend-20261020-000000",
            "0" * 64,
            "true",
            "",
            "e7a13c9b40d2",
            reviewed,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == expected, result.stderr + result.stdout


def test_revision_backup_reads_database_when_old_image_cannot_resolve_new_revision(tmp_path):
    import subprocess

    source = (SCRIPT.parents[1] / "deploy/update-backend.sh").read_text()
    record = next(line for line in source.splitlines() if line.endswith('> "$backup/revision.before"'))
    prefix = r"""
set -Eeuo pipefail
backup="$1"
sudo() {
  case "$*" in
    *"api alembic current") echo "Can't locate revision identified by 9c35e1a62f4" >&2; return 1 ;;
    *"postgres psql"*) printf '9c35e1a62f4\n' ;;
    *) return 9 ;;
  esac
}
"""
    result = subprocess.run(
        ["bash", "-c", prefix + record, "fixture", str(tmp_path)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "revision.before").read_text() == "9c35e1a62f4\n"
