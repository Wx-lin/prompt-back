"""Local-only tests for the hidden-input WeChat credential configuration tool."""

import importlib.util
import io
import json
import os
import secrets
import shlex
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "configure_wechat.py"
APP_ID = "wx0123456789abcdef"
# Synthetic value used only by isolated tests; never sent to WeChat.
SECRET = secrets.token_hex(24)


@pytest.fixture
def tool():
    assert SCRIPT.is_file(), "The secure WeChat configuration tool has not been implemented"
    spec = importlib.util.spec_from_file_location("configure_wechat_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def safe_result(*, configured=False):
    return {
        "app_id": APP_ID if configured else None,
        "app_id_present": configured,
        "app_id_matches": configured,
        "secret_present": configured,
        "backup_path": (
            "/opt/aigc-prompt/releases/wechat-config-20261020T120000Z-abcdefgh/runtime.env"
            if configured
            else None
        ),
    }


def capture_ssh(monkeypatch, tool, result=None):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=json.dumps(result or safe_result()), stderr="")

    monkeypatch.setattr(tool.subprocess, "run", run)
    return calls


def interactive(monkeypatch, tool, value=SECRET):
    monkeypatch.setattr(tool.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(tool.getpass, "getpass", lambda _prompt: value)


def remote(tool, tmp_path, content=b"DATABASE_URL=preserved\nWECHAT_APP_SECRET=\n"):
    namespace = {"__name__": "wechat_remote_tests"}
    exec(compile(tool.REMOTE_SOURCE, "wechat_remote", "exec"), namespace)
    namespace["ROOT"] = tmp_path
    if content is not None:
        (tmp_path / "runtime.env").write_bytes(content)
    return namespace


def configure_payload():
    return {"action": "configure", "app_id": APP_ID, "secret": SECRET}


def test_default_check_has_no_prompt_and_uses_safe_ssh_options(tool, monkeypatch, capsys):
    calls = capture_ssh(monkeypatch, tool)
    monkeypatch.setattr(tool.getpass, "getpass", lambda _prompt: pytest.fail("check prompted"))
    assert tool.main(["--app-id", APP_ID, "--socket", "/tmp/test-control.sock"]) == 0
    command, kwargs = calls[0]
    assert command[:8] == [
        "ssh",
        "-T",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
    ]
    assert command[8:10] == ["-S", "/tmp/test-control.sock"]
    assert command[-2] == "ubuntu@62.234.69.93"
    assert command[-1].startswith("python3 -c '")
    assert shlex.split(command[-1]) == ["python3", "-c", tool.REMOTE_SOURCE]
    assert kwargs["timeout"] == 30
    assert kwargs["capture_output"] is True
    assert json.loads(kwargs["input"]) == {"action": "check", "app_id": APP_ID}
    assert json.loads(capsys.readouterr().out) == safe_result()


def test_configure_secret_is_only_stdin_never_argv_or_logs(tool, monkeypatch, capsys):
    interactive(monkeypatch, tool)
    calls = capture_ssh(monkeypatch, tool, safe_result(configured=True))
    assert tool.main(["--app-id", APP_ID, "--configure"]) == 0
    command, kwargs = calls[0]
    assert SECRET not in repr(command)
    assert "env" not in kwargs
    assert json.loads(kwargs["input"]) == configure_payload()
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert json.loads(output.out)["secret_present"] is True


def test_noninteractive_configure_never_falls_back_to_echoed_input(tool, monkeypatch, capsys):
    monkeypatch.setattr(tool.sys, "stdin", io.StringIO())
    monkeypatch.setattr(tool.getpass, "getpass", lambda _prompt: pytest.fail("unsafe input"))
    monkeypatch.setattr(tool.subprocess, "run", lambda *_a, **_k: pytest.fail("ssh called"))
    assert tool.main(["--app-id", APP_ID, "--configure"]) == 1
    assert "终端" in capsys.readouterr().err


@pytest.mark.parametrize("value", ["x" * 31, "x" * 129, "含有中文" * 10, "x" * 32 + "\n", "x" * 32 + ";"])
def test_invalid_secret_is_rejected_without_leaking_value(tool, monkeypatch, capsys, value):
    interactive(monkeypatch, tool, value)
    monkeypatch.setattr(tool.subprocess, "run", lambda *_a, **_k: pytest.fail("ssh called"))
    assert tool.main(["--app-id", APP_ID, "--configure"]) == 1
    output = capsys.readouterr()
    assert value not in output.out + output.err


def test_cli_rejects_secret_argument_without_echoing_its_value(tool, capsys):
    with pytest.raises(SystemExit) as error:
        tool.main(["--app-id", APP_ID, "--secret", SECRET])
    assert error.value.code == 2
    assert SECRET not in "".join(capsys.readouterr())


def test_hidden_input_warning_never_falls_back_to_echo(tool, monkeypatch, capsys):
    interactive(monkeypatch, tool)

    def warning(_prompt):
        import warnings

        warnings.warn(SECRET, tool.getpass.GetPassWarning, stacklevel=2)

    monkeypatch.setattr(tool.getpass, "getpass", warning)
    monkeypatch.setattr(tool.subprocess, "run", lambda *_a, **_k: pytest.fail("ssh called"))
    assert tool.main(["--app-id", APP_ID, "--configure"]) == 1
    assert SECRET not in "".join(capsys.readouterr())


def test_subprocess_value_error_text_is_not_trusted(tool, monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise ValueError("AppSecret 格式无效" + SECRET)

    monkeypatch.setattr(tool.subprocess, "run", fail)
    assert tool.main(["--app-id", APP_ID]) == 1
    assert SECRET not in "".join(capsys.readouterr())


@pytest.mark.parametrize("field", ["app_id", "secret_present", "backup_path", "secret"])
def test_structured_server_output_cannot_echo_unexpected_secret(tool, monkeypatch, capsys, field):
    result = {**safe_result(), field: SECRET}
    capture_ssh(monkeypatch, tool, result)
    assert tool.main(["--app-id", APP_ID, "--check"]) == 1
    assert SECRET not in "".join(capsys.readouterr())


def test_recognized_remote_failure_is_safe_and_actionable(tool, monkeypatch, capsys):
    monkeypatch.setattr(
        tool.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=1, stdout=SECRET, stderr=json.dumps({"error": "secret_exists"})
        ),
    )
    assert tool.main(["--app-id", APP_ID]) == 1
    output = capsys.readouterr()
    assert "拒绝覆盖" in output.err
    assert SECRET not in output.out + output.err


@pytest.mark.parametrize("kind", ["failed", "timeout", "malformed"])
def test_ssh_errors_and_untrusted_output_are_redacted(tool, monkeypatch, capsys, kind):
    def run(command, **_kwargs):
        if kind == "timeout":
            raise subprocess.TimeoutExpired(command, 30, output=SECRET, stderr=SECRET)
        return SimpleNamespace(returncode=255 if kind == "failed" else 0, stdout=SECRET, stderr=SECRET)

    monkeypatch.setattr(tool.subprocess, "run", run)
    assert tool.main(["--app-id", APP_ID]) == 1
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    assert output.out == ""


def test_remote_check_outputs_only_presence_and_valid_app_id(tool, tmp_path):
    namespace = remote(tool, tmp_path, b"WECHAT_APP_ID=not-an-app-id\nWECHAT_APP_SECRET=" + SECRET.encode())
    before = (tmp_path / "runtime.env").read_bytes()
    result = namespace["remote_execute"]({"action": "check", "app_id": APP_ID})
    assert result == {**safe_result(), "app_id_present": True, "secret_present": True}
    assert SECRET not in json.dumps(result)
    assert (tmp_path / "runtime.env").read_bytes() == before
    assert not (tmp_path / "releases").exists()


@pytest.mark.parametrize("value", ["# not configured", "  # not configured"])
def test_remote_check_does_not_treat_app_id_comment_as_value(tool, tmp_path, value):
    namespace = remote(tool, tmp_path, f"WECHAT_APP_ID={value}\nWECHAT_APP_SECRET=\n".encode())
    assert namespace["remote_execute"]({"action": "check", "app_id": APP_ID}) == safe_result()


def test_remote_configure_preserves_other_bytes_and_makes_private_backup(tool, tmp_path):
    original = (
        b"# keep\r\nDATABASE_URL=preserved\r\nexport WECHAT_APP_ID=''\r\n"
        b"WECHAT_APP_SECRET=\r\nTAIL=no-newline"
    )
    namespace = remote(tool, tmp_path, original)
    releases = tmp_path / "releases"
    releases.mkdir(mode=0o755)
    releases.chmod(0o755)
    result = namespace["remote_execute"](configure_payload())
    expected = (
        b"# keep\r\nDATABASE_URL=preserved\r\nWECHAT_APP_ID="
        + APP_ID.encode()
        + b"\r\nWECHAT_APP_SECRET="
        + SECRET.encode()
        + b"\r\nTAIL=no-newline"
    )
    assert (tmp_path / "runtime.env").read_bytes() == expected
    backup = Path(result["backup_path"])
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(releases.stat().st_mode) == 0o755
    assert stat.S_IMODE((tmp_path / "runtime.env").stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".runtime.env-wechat-*"))


def test_remote_new_backup_parent_is_private_even_with_permissive_umask(tool, tmp_path):
    namespace = remote(tool, tmp_path, b"OTHER=keep")
    previous = os.umask(0)
    try:
        result = namespace["remote_execute"](configure_payload())
    finally:
        os.umask(previous)
    assert (tmp_path / "runtime.env").read_bytes().startswith(b"OTHER=keep\n")
    assert stat.S_IMODE((tmp_path / "releases").stat().st_mode) == 0o700
    assert stat.S_IMODE(Path(result["backup_path"]).stat().st_mode) == 0o600


@pytest.mark.parametrize("value", [SECRET, "'already-set'", '"already-set" # comment', '" "', '"" ""'])
def test_remote_refuses_any_existing_nonempty_secret(tool, tmp_path, value):
    original = f"WECHAT_APP_SECRET=\nexport WECHAT_APP_SECRET={value}\n".encode()
    namespace = remote(tool, tmp_path, original)
    expected = "ambiguous_environment" if value == '"" ""' else "secret_exists"
    with pytest.raises(namespace["RemoteError"], match=expected):
        namespace["remote_execute"](configure_payload())
    assert (tmp_path / "runtime.env").read_bytes() == original
    assert not (tmp_path / "releases").exists()


@pytest.mark.parametrize("key", ["wechat_app_secret", "WeChat_App_Secret", "'WECHAT_APP_SECRET'"])
def test_remote_refuses_existing_secret_case_insensitively(tool, tmp_path, key):
    original = f"{key}={SECRET}\n".encode()
    namespace = remote(tool, tmp_path, original)
    with pytest.raises(namespace["RemoteError"], match="secret_exists"):
        namespace["remote_execute"](configure_payload())
    assert (tmp_path / "runtime.env").read_bytes() == original


def test_remote_check_recognizes_settings_case_insensitivity(tool, tmp_path):
    namespace = remote(tool, tmp_path, f"wechat_app_id={APP_ID}\nWeChat_App_Secret={SECRET}\n".encode())
    result = namespace["remote_execute"]({"action": "check", "app_id": APP_ID})
    assert result["app_id"] == APP_ID
    assert result["app_id_matches"] is True
    assert result["secret_present"] is True


@pytest.mark.parametrize(
    "line", ["WECHAT_APP_SECRET", "'WECHAT_APP_SECRET=existing", "WECHAT_APP_SECRET : existing"]
)
def test_remote_refuses_ambiguous_target_key_syntax(tool, tmp_path, line):
    original = (line + "\nOTHER=keep\n").encode()
    namespace = remote(tool, tmp_path, original)
    with pytest.raises(namespace["RemoteError"], match="ambiguous_environment"):
        namespace["remote_execute"](configure_payload())
    assert (tmp_path / "runtime.env").read_bytes() == original
    assert not (tmp_path / "releases").exists()


@pytest.mark.parametrize("quote", ['"', "'"])
def test_remote_refuses_multiline_values_instead_of_rewriting_their_contents(tool, tmp_path, quote):
    original = f"OTHER={quote}preserve first line\nWECHAT_APP_SECRET=\npreserve last line{quote}\n".encode()
    namespace = remote(tool, tmp_path, original)
    with pytest.raises(namespace["RemoteError"], match="ambiguous_environment"):
        namespace["remote_execute"](configure_payload())
    assert (tmp_path / "runtime.env").read_bytes() == original
    assert not (tmp_path / "releases").exists()


def test_remote_refuses_ambiguous_escaped_quote_context(tool, tmp_path):
    original = b'OTHER="first line\\\\"\nWECHAT_APP_SECRET=\nlast line"\n'
    namespace = remote(tool, tmp_path, original)
    with pytest.raises(namespace["RemoteError"], match="ambiguous_environment"):
        namespace["remote_execute"](configure_payload())
    assert (tmp_path / "runtime.env").read_bytes() == original


@pytest.mark.parametrize("value", ["", "''", '"" # empty', "  # empty"])
def test_remote_accepts_empty_secret_values(tool, tmp_path, value):
    namespace = remote(tool, tmp_path, f"WECHAT_APP_SECRET={value}\n".encode())
    result = namespace["remote_execute"](configure_payload())
    assert result["secret_present"] is True


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "missing"])
def test_remote_refuses_unsafe_or_missing_environment(tool, tmp_path, kind):
    namespace = remote(tool, tmp_path, None)
    env = tmp_path / "runtime.env"
    if kind == "symlink":
        target = tmp_path / "target"
        target.write_text("OTHER=untouched")
        env.symlink_to(target)
    elif kind == "directory":
        env.mkdir()
    elif kind == "fifo":
        os.mkfifo(env)
    with pytest.raises(namespace["RemoteError"]):
        namespace["remote_execute"](configure_payload())
    assert not (tmp_path / "releases").exists()


def test_remote_missing_check_reports_false_without_creating_environment(tool, tmp_path):
    namespace = remote(tool, tmp_path, None)
    assert namespace["remote_execute"]({"action": "check", "app_id": APP_ID}) == safe_result()
    assert not (tmp_path / "runtime.env").exists()


def test_remote_main_check_prints_presence_without_secret(tool, tmp_path, monkeypatch, capsys):
    namespace = remote(tool, tmp_path, f"WECHAT_APP_ID={APP_ID}\nWECHAT_APP_SECRET={SECRET}\n".encode())
    monkeypatch.setattr(
        namespace["sys"], "stdin", io.StringIO(json.dumps({"action": "check", "app_id": APP_ID}))
    )
    assert namespace["remote_main"]() == 0
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    result = json.loads(output.out)
    assert result["app_id"] == APP_ID
    assert result["secret_present"] is True


@pytest.mark.parametrize("raw", [SECRET, "x" * 4097])
def test_remote_main_invalid_json_never_echoes_input(tool, tmp_path, monkeypatch, capsys, raw):
    namespace = remote(tool, tmp_path)
    monkeypatch.setattr(namespace["sys"], "stdin", io.StringIO(raw))
    assert namespace["remote_main"]() == 1
    assert raw not in "".join(capsys.readouterr())


def test_remote_lock_is_shared_and_nonblocking(tool, tmp_path):
    import fcntl

    namespace = remote(tool, tmp_path)
    with (tmp_path / ".release-deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(namespace["RemoteError"], match="lock_busy"):
            namespace["remote_execute"](configure_payload())
    assert not (tmp_path / "releases").exists()


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "configure", "app_id": APP_ID, "secret": "bad"},
        {"action": "configure", "app_id": "bad", "secret": SECRET},
        {**configure_payload(), "path": "/tmp/other"},
        {"action": "overwrite", "app_id": APP_ID},
    ],
)
def test_remote_revalidates_input_before_touching_files(tool, tmp_path, payload):
    namespace = remote(tool, tmp_path)
    with pytest.raises(namespace["RemoteError"], match="invalid_input"):
        namespace["remote_execute"](payload)
    assert not (tmp_path / ".release-deploy.lock").exists()


def test_remote_atomic_write_failure_preserves_original_and_redacts_exception(
    tool, tmp_path, monkeypatch, capsys
):
    original = b"OTHER=untouched\n"
    namespace = remote(tool, tmp_path, original)

    def fail_replace(*_args):
        raise OSError(SECRET)

    monkeypatch.setattr(namespace["os"], "replace", fail_replace)
    monkeypatch.setattr(namespace["sys"], "stdin", io.StringIO(json.dumps(configure_payload())))
    assert namespace["remote_main"]() == 1
    assert (tmp_path / "runtime.env").read_bytes() == original
    assert SECRET not in "".join(capsys.readouterr())
    assert not list(tmp_path.glob(".runtime.env-wechat-*"))
