#!/usr/bin/env python3
"""Run real prompt code against explicitly isolated local persistence.

This launcher never migrates a database, submits a task, or changes a remote
service. An optional read-only SSH operation imports only the vision provider's
configuration into child-process memory; no signing key is copied or logged.
"""

import argparse
import json
import os
import re
import secrets
import shlex
import signal
import stat
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MODEL_KEYS = (
    "PROMPT_VISION_PROVIDER", "PROMPT_VISION_BASE_URL", "PROMPT_VISION_API_KEY",
    "PROMPT_VISION_MODEL", "PROMPT_VISION_MAX_TOKENS", "PROMPT_ANALYSIS_PROMPT_VERSION",
)
CONTAINER_SOURCE = (
    "import json,os; print(json.dumps({key:os.environ.get(key,'') "
    f"for key in {MODEL_KEYS!r}}}))"
)
# Verify the already-known machine identity before reading its fixed container.
REMOTE_SOURCE = f"""
import getpass,socket,subprocess,sys
if socket.gethostname() != 'VM-0-5-ubuntu' or getpass.getuser() != 'ubuntu':
    sys.exit(1)
result = subprocess.run(
    ['sudo','-n','docker','exec','aigc-prompt-api-1','python','-c',{CONTAINER_SOURCE!r}],
    capture_output=True,text=True,timeout=15,
)
if result.returncode or len(result.stdout) > 16384:
    sys.exit(1)
sys.stdout.write(result.stdout)
"""


class StartupError(Exception):
    """Messages are fixed safe diagnostics, never raw config or child output."""


class SafeParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default message echoes unknown arguments, possibly secrets.
        super().error("invalid local preview arguments")


def _local_url(value, scheme, label):
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != scheme or parsed.hostname not in LOOPBACK
            or parsed.query or parsed.fragment or parsed.port == 0
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError
    except (TypeError, ValueError):
        raise StartupError(f"Unsafe local {label} target") from None
    return parsed


def validate_database_url(value):
    parsed = _local_url(value, "postgresql+asyncpg", "database")
    if not re.fullmatch(r"/aigc_prompt_preview_[a-z0-9_]+", parsed.path):
        raise StartupError("Unsafe local database target")
    return value


def validate_redis_url(value):
    parsed = _local_url(value, "redis", "Redis")
    if not re.fullmatch(r"/(?:[1-9]|1[0-5])", parsed.path):
        raise StartupError("Unsafe local Redis target")
    return value


def validate_control_socket(value):
    path = Path(value)
    try:
        parent, control = path.parent.lstat(), path.lstat()
        if (
            not path.is_absolute() or not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700
            or parent.st_uid != os.geteuid() or control.st_uid != os.geteuid()
            or not stat.S_ISSOCK(control.st_mode)
        ):
            raise ValueError
    except (OSError, ValueError):
        raise StartupError("Unsafe SSH control socket") from None
    return str(path)


def _model_values(value):
    if not isinstance(value, dict) or set(value) != set(MODEL_KEYS):
        raise ValueError
    if any(not isinstance(item, str) or len(item) > 4096 for item in value.values()):
        raise ValueError
    if any(not item.isprintable() for item in value.values() if item):
        raise ValueError
    base = urlsplit(value["PROMPT_VISION_BASE_URL"])
    if (
        value["PROMPT_VISION_PROVIDER"] not in {"dashscope", "openai-compatible"}
        or base.scheme != "https" or not base.hostname or base.username or base.password
        or base.query or base.fragment or not value["PROMPT_VISION_API_KEY"].strip()
        or not value["PROMPT_VISION_MODEL"].strip()
        or len(value["PROMPT_VISION_MODEL"]) > 200
        or value["PROMPT_ANALYSIS_PROMPT_VERSION"] not in {"", "v1"}
    ):
        raise ValueError
    tokens = value["PROMPT_VISION_MAX_TOKENS"] or "4096"
    if not tokens.isdigit() or not 256 <= int(tokens) <= 16384:
        raise ValueError
    return {
        **value, "PROMPT_VISION_MAX_TOKENS": tokens,
        "PROMPT_ANALYSIS_PROMPT_VERSION": "v1",
    }


def read_model_configuration(control_socket):
    control = validate_control_socket(control_socket)
    command = [
        "ssh", "-T", "-S", control, "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
        "ubuntu@62.234.69.93", "python3 -c " + shlex.quote(REMOTE_SOURCE),
    ]
    try:
        result = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
        )
        if result.returncode or len(result.stdout) > 16384:
            raise ValueError
        return _model_values(json.loads(result.stdout))
    except (OSError, ValueError, subprocess.SubprocessError):
        raise StartupError("Model configuration unavailable or invalid") from None


def child_environment(args, model=None, parent=None):
    return {
        **(os.environ if parent is None else parent),
        **(model or {}),
        "AIGC_LOCAL_PROMPT_PREVIEW": "1",
        "APP_ENV": "local-prompt-preview",
        "DATABASE_URL": validate_database_url(args.database_url),
        "REDIS_URL": validate_redis_url(args.redis_url),
        "PROMPT_ANALYSIS_ENABLED": "true",
        "PROMPT_ANALYSIS_WORKER_ENABLED": "true",
        "GSC_ENABLED": "false",
        # This runtime verifies existing sessions with its upstream. It must not
        # accidentally sign production sessions using an inherited .env value.
        "JWT_SECRET_KEY": secrets.token_urlsafe(48),
        "ADMIN_SECRET_KEY": secrets.token_urlsafe(48),
    }


def _stop_children(children):
    alive = tuple(child for child in children if child.poll() is None)
    for child in alive:
        child.send_signal(signal.SIGTERM)
    for child in alive:
        try:
            child.wait(timeout=130)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)


def supervise(args, environment):
    stopped = threading.Event()
    previous = {sig: signal.signal(sig, lambda *_args: stopped.set())
                for sig in (signal.SIGINT, signal.SIGTERM)}
    children = ()
    commands = (
        [sys.executable, "-m", "uvicorn", "scripts.local_prompt_preview:create_app", "--factory",
         "--host", "127.0.0.1", "--port", str(args.port), "--no-access-log"],
        [sys.executable, "-m", "app.workers.prompt_analysis"],
    )
    try:
        for command in commands:
            children = (*children, subprocess.Popen(command, cwd=ROOT, env=environment))
        while not stopped.is_set():
            for child in children:
                code = child.poll()
                if code is not None:
                    return code if code > 0 else 1
            stopped.wait(0.2)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        _stop_children(children)
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main(argv=None):
    parser = SafeParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--redis-url", required=True)
    parser.add_argument("--port", type=int, default=8040)
    parser.add_argument("--model-config-from-ssh", metavar="CONTROL_SOCKET")
    args = parser.parse_args(argv)
    try:
        validate_database_url(args.database_url)
        validate_redis_url(args.redis_url)
        if args.port != 8040:
            raise StartupError("Invalid loopback API port")
        model = read_model_configuration(args.model_config_from_ssh) if args.model_config_from_ssh else None
        environment = child_environment(args, model)
        print(f"Starting local prompt preview at http://127.0.0.1:{args.port}; no task is created.")
        return supervise(args, environment)
    except StartupError as error:
        print(str(error), file=sys.stderr)
    except (OSError, subprocess.SubprocessError):
        print("Local prompt preview could not start.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
