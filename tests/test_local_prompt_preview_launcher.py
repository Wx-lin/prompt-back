"""Launcher safety checks never contact SSH, COS, a model, or a database."""

import getpass
import importlib.util
import json
import os
import signal
import socket
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "run_local_prompt_preview.py"
DATABASE = "postgresql+asyncpg://preview@127.0.0.1:5432/aigc_prompt_preview_test"
REDIS = "redis://127.0.0.1:6379/12"
MODEL = {
    "PROMPT_VISION_PROVIDER": "dashscope",
    "PROMPT_VISION_BASE_URL": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "PROMPT_VISION_API_KEY": "synthetic-model-key-for-isolated-tests",
    "PROMPT_VISION_MODEL": "qwen3.8-max",
    "PROMPT_VISION_MAX_TOKENS": "4096",
    "PROMPT_ANALYSIS_PROMPT_VERSION": "v1",
}


@pytest.fixture
def launcher():
    assert SCRIPT.is_file(), "The isolated local preview launcher is not implemented"
    spec = importlib.util.spec_from_file_location("local_preview_launcher_tests", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def control_socket():
    # macOS's sun_path limit is shorter than pytest's descriptive temp paths.
    with tempfile.TemporaryDirectory(prefix="lp-", dir="/tmp") as directory:
        address = Path(directory) / "control"
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(address))
        try:
            yield address
        finally:
            listener.close()


@pytest.mark.parametrize("url", [
    "postgresql+asyncpg://preview@127.0.0.1/aigc_prompt",
    "postgresql+asyncpg://preview@db.example/aigc_prompt_preview_test",
    "postgresql://preview@127.0.0.1/aigc_prompt_preview_test",
    DATABASE + "?database=aigc_prompt",
    DATABASE + "#fragment",
    "postgresql+asyncpg://preview@127.0.0.1/aigc_prompt_preview_%2fother",
])
def test_database_requires_an_explicit_loopback_preview_database(launcher, url):
    with pytest.raises(launcher.StartupError):
        launcher.validate_database_url(url)


@pytest.mark.parametrize("url", [
    "redis://127.0.0.1:6379/0", "redis://remote.example:6379/12",
    "redis://127.0.0.1:6379/16",
    REDIS + "?db=0", REDIS + "#fragment", "redis://127.0.0.1:6379/not-a-db",
])
def test_redis_requires_an_explicit_loopback_nonzero_database(launcher, url):
    with pytest.raises(launcher.StartupError):
        launcher.validate_redis_url(url)


def test_accepts_only_the_local_targets_and_uses_a_private_socket(launcher, control_socket):
    assert launcher.validate_database_url(DATABASE) == DATABASE
    assert launcher.validate_redis_url(REDIS) == REDIS
    assert launcher.validate_control_socket(control_socket) == str(control_socket)


def test_rejects_socket_symlinks_and_nonprivate_directories(launcher, control_socket):
    alias = control_socket.parent / "alias"
    alias.symlink_to(control_socket)
    with pytest.raises(launcher.StartupError):
        launcher.validate_control_socket(alias)
    control_socket.parent.chmod(0o755)
    with pytest.raises(launcher.StartupError):
        launcher.validate_control_socket(control_socket)


def test_rejects_a_socket_owned_by_another_user(launcher, control_socket, monkeypatch):
    monkeypatch.setattr(launcher.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises(launcher.StartupError):
        launcher.validate_control_socket(control_socket)


def test_ssh_reads_only_fixed_model_configuration_without_logging_it(
    launcher, control_socket, monkeypatch, capsys,
):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(MODEL), stderr="")

    monkeypatch.setattr(launcher.subprocess, "run", run)
    assert launcher.read_model_configuration(control_socket) == MODEL
    command, kwargs = calls[0]
    assert command[-2] == "ubuntu@62.234.69.93"
    assert command[command.index("-S") + 1] == str(control_socket)
    assert "BatchMode=yes" in command and "StrictHostKeyChecking=yes" in command
    assert kwargs["capture_output"] is True and kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["timeout"] == 30
    assert "aigc-prompt-api-1" in command[-1]
    assert "VM-0-5-ubuntu" in command[-1]
    assert "JWT_SECRET_KEY" not in command[-1]
    assert MODEL["PROMPT_VISION_API_KEY"] not in repr(command)
    assert MODEL["PROMPT_VISION_API_KEY"] not in "".join(capsys.readouterr())


@pytest.mark.parametrize("bad", [
    {**MODEL, "JWT_SECRET_KEY": "must-not-be-imported"},
    {**MODEL, "PROMPT_VISION_API_KEY": ""},
    {**MODEL, "PROMPT_VISION_PROVIDER": "other"},
    {**MODEL, "PROMPT_VISION_BASE_URL": "http://insecure.example/v1"},
    {**MODEL, "PROMPT_VISION_BASE_URL": "https://key@example.com/v1"},
    {**MODEL, "PROMPT_VISION_MAX_TOKENS": "999999"},
])
def test_rejects_unexpected_or_invalid_model_values_without_leaking(
    launcher, control_socket, monkeypatch, bad, capsys,
):
    monkeypatch.setattr(launcher.subprocess, "run", lambda *_a, **_k: SimpleNamespace(
        returncode=0, stdout=json.dumps(bad), stderr=MODEL["PROMPT_VISION_API_KEY"],
    ))
    with pytest.raises(launcher.StartupError) as error:
        launcher.read_model_configuration(control_socket)
    assert MODEL["PROMPT_VISION_API_KEY"] not in str(error.value)
    assert MODEL["PROMPT_VISION_API_KEY"] not in "".join(capsys.readouterr())


def test_ssh_failure_does_not_echo_remote_output(launcher, control_socket, monkeypatch):
    monkeypatch.setattr(launcher.subprocess, "run", lambda *_a, **_k: SimpleNamespace(
        returncode=1, stdout=MODEL["PROMPT_VISION_API_KEY"], stderr="private remote output",
    ))
    with pytest.raises(launcher.StartupError, match="Model configuration unavailable"):
        launcher.read_model_configuration(control_socket)


def test_remote_source_is_valid_and_checks_identity_before_container_read(launcher, monkeypatch):
    compile(launcher.CONTAINER_SOURCE, "container-source", "exec")
    compile(launcher.REMOTE_SOURCE, "remote-source", "exec")
    monkeypatch.setattr(socket, "gethostname", lambda: "wrong-machine")
    monkeypatch.setattr(getpass, "getuser", lambda: "ubuntu")
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("read untrusted host"))
    with pytest.raises(SystemExit):
        exec(launcher.REMOTE_SOURCE, {})


def test_ssh_transport_exception_is_sanitized(launcher, control_socket, monkeypatch):
    def fail(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("ssh", 30, output=MODEL["PROMPT_VISION_API_KEY"])

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(launcher.StartupError) as error:
        launcher.read_model_configuration(control_socket)
    assert MODEL["PROMPT_VISION_API_KEY"] not in str(error.value)


def test_environment_is_isolated_and_keeps_parent_mapping_unchanged(launcher):
    parent = {"DATABASE_URL": "production", "REDIS_URL": "production", "JWT_SECRET_KEY": "old"}
    args = SimpleNamespace(database_url=DATABASE, redis_url=REDIS, port=8040)
    environment = launcher.child_environment(args, MODEL, parent)
    assert parent == {"DATABASE_URL": "production", "REDIS_URL": "production", "JWT_SECRET_KEY": "old"}
    assert environment["DATABASE_URL"] == DATABASE and environment["REDIS_URL"] == REDIS
    assert environment["AIGC_LOCAL_PROMPT_PREVIEW"] == "1"
    assert environment["APP_ENV"] != "production"
    assert environment["PROMPT_ANALYSIS_ENABLED"] == "true"
    assert environment["PROMPT_ANALYSIS_WORKER_ENABLED"] == "true"
    assert environment["GSC_ENABLED"] == "false"
    assert environment["JWT_SECRET_KEY"] != "old"
    assert environment["PROMPT_VISION_MODEL"] == MODEL["PROMPT_VISION_MODEL"]


class Child:
    def __init__(self, status=None, timeout=False):
        self.status = status
        self.timeout = timeout
        self.signals = []
        self.killed = False

    def poll(self):
        return self.status

    def send_signal(self, sig):
        self.signals.append(sig)

    def wait(self, timeout):
        if self.timeout and not self.killed:
            raise subprocess.TimeoutExpired("synthetic-child", timeout)
        return 0

    def kill(self):
        self.killed = True


def test_supervisor_starts_only_loopback_api_and_same_code_worker_and_stops_both(
    launcher, monkeypatch,
):
    children = [Child(), Child(status=3)]
    calls = []

    def popen(command, **kwargs):
        child = children[len(calls)]
        calls.append((command, kwargs))
        return child

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher.signal, "signal", lambda *_a: signal.SIG_DFL)
    args = SimpleNamespace(port=8040)
    assert launcher.supervise(args, {"isolated": "yes"}) == 3
    api, worker = calls
    assert api[0][1:] == [
        "-m", "uvicorn", "scripts.local_prompt_preview:create_app", "--factory",
        "--host", "127.0.0.1", "--port", "8040", "--no-access-log",
    ]
    assert worker[0][1:] == ["-m", "app.workers.prompt_analysis"]
    assert api[0][0] == worker[0][0]
    assert api[1]["env"] == worker[1]["env"] == {"isolated": "yes"}
    assert children[0].signals == [signal.SIGTERM]
    assert "alembic" not in repr(calls)


def test_supervisor_terminates_the_other_child_after_startup_failure(launcher, monkeypatch):
    first = Child(timeout=True)
    calls = []

    def popen(*_args, **_kwargs):
        if calls:
            raise OSError(MODEL["PROMPT_VISION_API_KEY"])
        calls.append(True)
        return first

    monkeypatch.setattr(launcher.subprocess, "Popen", popen)
    monkeypatch.setattr(launcher.signal, "signal", lambda *_a: signal.SIG_DFL)
    with pytest.raises(OSError):
        launcher.supervise(SimpleNamespace(port=8040), {})
    assert first.signals == [signal.SIGTERM] and first.killed


def test_signal_requests_graceful_shutdown_and_restores_handlers(launcher, monkeypatch):
    handlers = {}
    old_handlers = []
    children = [Child(), Child()]
    polls = []

    def install(sig, handler):
        handlers[sig] = handler
        old_handlers.append((sig, handler))
        return signal.SIG_DFL

    def poll():
        if not polls:
            polls.append(True)
            handlers[signal.SIGTERM](signal.SIGTERM, None)
        return None

    children[0].poll = poll
    pending = iter(children)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *_a, **_k: next(pending))
    monkeypatch.setattr(launcher.signal, "signal", install)
    assert launcher.supervise(SimpleNamespace(port=8040), {}) == 0
    assert all(child.signals == [signal.SIGTERM] for child in children)
    assert old_handlers[-2:] == [(signal.SIGINT, signal.SIG_DFL), (signal.SIGTERM, signal.SIG_DFL)]


def test_main_passes_only_validated_remote_values_to_children(launcher, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(
        launcher, "read_model_configuration", lambda value: MODEL if value == "/safe" else None,
    )
    monkeypatch.setattr(launcher, "supervise", lambda args, env: calls.append((args, env)) or 0)
    assert launcher.main([
        "--database-url", DATABASE, "--redis-url", REDIS, "--model-config-from-ssh", "/safe",
    ]) == 0
    assert calls[0][1]["PROMPT_VISION_API_KEY"] == MODEL["PROMPT_VISION_API_KEY"]
    assert MODEL["PROMPT_VISION_API_KEY"] not in "".join(capsys.readouterr())


@pytest.mark.parametrize("port", ["80", "8041"])
def test_main_rejects_invalid_port_before_any_children_or_ssh(launcher, monkeypatch, capsys, port):
    monkeypatch.setattr(launcher, "read_model_configuration", lambda *_a: pytest.fail("SSH called"))
    monkeypatch.setattr(launcher, "supervise", lambda *_a: pytest.fail("children called"))
    assert launcher.main([
        "--database-url", DATABASE, "--redis-url", REDIS, "--port", port,
        "--model-config-from-ssh", "/safe",
    ]) == 1
    assert "Invalid loopback API port" in capsys.readouterr().err


def test_main_sanitizes_startup_failures_and_never_prints_configuration(
    launcher, monkeypatch, capsys,
):
    monkeypatch.setattr(launcher, "supervise", lambda *_a: (_ for _ in ()).throw(
        OSError(MODEL["PROMPT_VISION_API_KEY"]),
    ))
    assert launcher.main(["--database-url", DATABASE, "--redis-url", REDIS]) == 1
    assert MODEL["PROMPT_VISION_API_KEY"] not in "".join(capsys.readouterr())


def test_main_requires_explicit_targets_and_safe_cli_errors(launcher, capsys):
    with pytest.raises(SystemExit) as error:
        launcher.main(["--unsupported-secret", MODEL["PROMPT_VISION_API_KEY"]])
    assert error.value.code == 2
    assert MODEL["PROMPT_VISION_API_KEY"] not in "".join(capsys.readouterr())
