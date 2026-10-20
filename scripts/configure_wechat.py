#!/usr/bin/env python3
"""Check WeChat configuration, or set it once through hidden terminal input.

No deployment, restart or migration is performed. AppSecret is accepted only
through getpass and sent to a fixed remote Python program through SSH stdin.
"""

import argparse
import getpass
import json
import re
import shlex
import subprocess
import sys
import warnings

APP_ID_PATTERN = r"wx[0-9a-fA-F]{16}"
SECRET_PATTERN = r"[A-Za-z0-9_-]{32,128}"
BACKUP_PATTERN = r"/opt/aigc-prompt/releases/wechat-config-\d{8}T\d{6}Z-[a-z0-9_]{8}/runtime\.env"

# This source is public code, never interpolated with credentials. The only
# remote path is fixed here; request JSON cannot supply filesystem locations.
REMOTE_SOURCE = r"""
import fcntl
import json
import os
import re
import shlex
import stat
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/opt/aigc-prompt")
KEY_PATTERN = re.compile(
    rb"^[ \t]*(?:export[ \t]+)?(?P<quote>'?)(?P<key>WECHAT_APP_ID|WECHAT_APP_SECRET)"
    rb"(?P=quote)[ \t]*=[ \t]*(?P<value>.*)$", re.IGNORECASE
)
TARGET_PATTERN = re.compile(rb"\bWECHAT_APP_(?:ID|SECRET)\b", re.IGNORECASE)
ASSIGNMENT_PATTERN = re.compile(
    rb"^[ \t]*(?:export[ \t]+)?(?:'[^']+'|[^\s=#]+)[ \t]*=[ \t]*(.*)$", re.IGNORECASE
)
APP_ID_PATTERN = r"wx[0-9a-fA-F]{16}"
SECRET_PATTERN = r"[A-Za-z0-9_-]{32,128}"
MAX_ENV_BYTES = 4 * 1024 * 1024


class RemoteError(Exception):
    pass


def validate_payload(payload):
    if not isinstance(payload, dict):
        raise RemoteError("invalid_input")
    action = payload.get("action")
    expected = {"action", "app_id", "secret"} if action == "configure" else {"action", "app_id"}
    if action not in ("check", "configure") or set(payload) != expected:
        raise RemoteError("invalid_input")
    if not isinstance(payload["app_id"], str) or not re.fullmatch(APP_ID_PATTERN, payload["app_id"]):
        raise RemoteError("invalid_input")
    if action == "configure" and (
        not isinstance(payload["secret"], str) or not re.fullmatch(SECRET_PATTERN, payload["secret"])
    ):
        raise RemoteError("invalid_input")


def safe_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RemoteError("unsafe_path")


def read_environment(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, None
    if not stat.S_ISREG(info.st_mode):
        raise RemoteError("unsafe_path")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise RemoteError("unsafe_path")
        content = stream.read(MAX_ENV_BYTES + 1)
    if len(content) > MAX_ENV_BYTES:
        raise RemoteError("unsafe_path")
    return content, opened


def reject_multiline_quotes(line):
    stripped = line.lstrip()
    if stripped.startswith(b"#"):
        return
    prefix = re.sub(rb"^export[ \t]+", b"", stripped, flags=re.IGNORECASE)
    if prefix.startswith(b"'") and not re.match(rb"^'[^']+'(?:[ \t]*=|[ \t]*(?:#.*)?$)", prefix):
        raise RemoteError("ambiguous_environment")
    assignment = ASSIGNMENT_PATTERN.fullmatch(line)
    if not assignment:
        return
    value = assignment[1]
    if value[:1] not in (b"'", b'"'):
        return
    quote = value[0]
    for index, character in enumerate(value[1:], start=1):
        if character == quote and value[index - 1] != 92:
            trailing = value[index + 1:].lstrip()
            if trailing and not trailing.startswith(b"#"):
                raise RemoteError("ambiguous_environment")
            return
    raise RemoteError("ambiguous_environment")


def environment_values(content):
    values = {"WECHAT_APP_ID": [], "WECHAT_APP_SECRET": []}
    for line in (content or b"").splitlines():
        reject_multiline_quotes(line)
        match = KEY_PATTERN.fullmatch(line)
        if not match and not line.lstrip().startswith(b"#") and TARGET_PATTERN.search(line.split(b"=", 1)[0]):
            raise RemoteError("ambiguous_environment")
        if match:
            key = match["key"].decode("ascii").upper()
            raw = match["value"].decode("utf-8", errors="replace")
            if key == "WECHAT_APP_SECRET":
                stripped = raw.strip()
                empty = not stripped or stripped.startswith("#") or re.fullmatch(
                    r"(?:''|\"\")[ \t]*(?:#.*)?", stripped
                )
                value = "" if empty else raw
            else:
                try:
                    tokens = shlex.split(raw, comments=True)
                    value = tokens[0] if len(tokens) == 1 else "" if not tokens else raw
                except ValueError:
                    value = raw
            values[key].append(value)
    return values


def safe_status(content, requested_id, backup_path=None):
    values = environment_values(content)
    app_id = values["WECHAT_APP_ID"][-1] if values["WECHAT_APP_ID"] else ""
    return {
        "app_id": app_id if re.fullmatch(APP_ID_PATTERN, app_id) else None,
        "app_id_present": any(values["WECHAT_APP_ID"]),
        "app_id_matches": app_id == requested_id,
        "secret_present": any(values["WECHAT_APP_SECRET"]),
        "backup_path": str(backup_path) if backup_path else None,
    }


def updated_environment(content, payload):
    newline = b"\r\n" if b"\r\n" in content else b"\n"
    replacements = {
        b"WECHAT_APP_ID": payload["app_id"].encode("ascii"),
        b"WECHAT_APP_SECRET": payload["secret"].encode("ascii"),
    }
    seen = set()
    result = []
    for line in content.splitlines(keepends=True):
        match = KEY_PATTERN.fullmatch(line.rstrip(b"\r\n"))
        if not match:
            result.append(line)
        elif match["key"].upper() not in seen:
            key = match["key"].upper()
            ending = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
            result.append(key + b"=" + replacements[key] + ending)
            seen.add(key)
    for key, value in replacements.items():
        if key not in seen:
            if result and not result[-1].endswith(b"\n"):
                result.append(newline)
            result.append(key + b"=" + value + newline)
    return b"".join(result)


def private_backup(content):
    releases = ROOT / "releases"
    try:
        releases.mkdir(mode=0o700)
    except FileExistsError:
        safe_directory(releases)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = Path(tempfile.mkdtemp(prefix="wechat-config-" + stamp + "-", dir=releases))
    directory.chmod(0o700)
    backup_path = directory / "runtime.env"
    descriptor = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    return backup_path


def atomic_write(path, updated, original, original_info):
    descriptor, temporary = tempfile.mkstemp(prefix=".runtime.env-wechat-", dir=ROOT)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            os.fchown(stream.fileno(), original_info.st_uid, original_info.st_gid)
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        current, current_info = read_environment(path)
        if current != original or current_info is None or (
            current_info.st_dev, current_info.st_ino
        ) != (original_info.st_dev, original_info.st_ino):
            raise RemoteError("environment_changed")
        os.replace(temporary, path)
        directory = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def remote_execute(payload):
    validate_payload(payload)
    safe_directory(ROOT)
    descriptor = os.open(
        ROOT / ".release-deploy.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
    )
    with os.fdopen(descriptor, "a") as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise RemoteError("unsafe_path")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RemoteError("lock_busy") from None
        path = ROOT / "runtime.env"
        original, info = read_environment(path)
        if payload["action"] == "check":
            return safe_status(original, payload["app_id"])
        if original is None:
            raise RemoteError("environment_missing")
        if any(environment_values(original)["WECHAT_APP_SECRET"]):
            raise RemoteError("secret_exists")
        updated = updated_environment(original, payload)
        backup_path = private_backup(original)
        atomic_write(path, updated, original, info)
        return safe_status(updated, payload["app_id"], backup_path)


def remote_main():
    try:
        raw = sys.stdin.read(4097)
        if len(raw) > 4096:
            raise RemoteError("invalid_input")
        result = remote_execute(json.loads(raw))
        print(json.dumps(result))
        return 0
    except RemoteError as error:
        print(json.dumps({"error": error.args[0]}), file=sys.stderr)
    except Exception:
        print(json.dumps({"error": "remote_failure"}), file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(remote_main())
"""

REMOTE_ERRORS = {
    "invalid_input": "配置输入格式无效，未更改服务器配置。",
    "unsafe_path": "服务器配置路径不安全，已拒绝操作。",
    "lock_busy": "服务器有另一项发布或配置操作，请稍后重试。",
    "environment_missing": "服务器 runtime.env 不存在，已拒绝创建残缺配置。",
    "secret_exists": "服务器已有非空 AppSecret，已拒绝覆盖；本工具不执行密钥轮换。",
    "environment_changed": "服务器环境文件已变化，已拒绝覆盖，请重新检查。",
    "ambiguous_environment": "服务器微信配置键格式不明确，已拒绝覆盖；请先人工核验格式。",
    "remote_failure": "服务器配置操作失败，请检查权限；没有显示敏感错误详情。",
}

INPUT_ERRORS = {
    "terminal_required": "请在 Mac 终端交互执行 --configure，输入不会回显。",
    "invalid_secret": "AppSecret 格式无效：仅允许 32–128 位 ASCII 字母、数字、下划线或短横线。",
}


class HiddenInputError(Exception):
    pass


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message):
        self.print_usage(sys.stderr)
        self.exit(2, "参数无效，请使用 --help；AppSecret 不能放在命令参数中。\n")


def arguments(argv):
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--app-id", required=True, help="公开的小程序 AppID")
    parser.add_argument("--host", default="ubuntu@62.234.69.93", help="SSH 目标主机")
    parser.add_argument("--socket", help="可选：已建立的 SSH ControlMaster socket")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="仅检查配置是否存在（默认）")
    mode.add_argument("--configure", action="store_true", help="在交互终端隐藏输入并首次配置")
    parsed = parser.parse_args(argv)
    if not re.fullmatch(APP_ID_PATTERN, parsed.app_id):
        parser.error("invalid app id")
    if not re.fullmatch(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9.-]*", parsed.host):
        parser.error("invalid ssh host")
    return parsed


def hidden_secret():
    if not sys.stdin.isatty():
        raise HiddenInputError("terminal_required")
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        secret = getpass.getpass("请输入微信 AppSecret（输入不回显）: ")
    if not re.fullmatch(SECRET_PATTERN, secret):
        raise HiddenInputError("invalid_secret")
    return secret


def ssh_command(args):
    command = [
        "ssh",
        "-T",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
    ]
    if args.socket:
        command.extend(["-S", args.socket])
    return [*command, args.host, "python3 -c " + shlex.quote(REMOTE_SOURCE)]


def sanitized_result(raw):
    result = json.loads(raw)
    expected = {"app_id", "app_id_present", "app_id_matches", "secret_present", "backup_path"}
    if not isinstance(result, dict) or set(result) != expected:
        raise ValueError
    if result["app_id"] is not None and (
        not isinstance(result["app_id"], str) or not re.fullmatch(APP_ID_PATTERN, result["app_id"])
    ):
        raise ValueError
    for field in ("app_id_present", "app_id_matches", "secret_present"):
        if type(result[field]) is not bool:
            raise ValueError
    if result["backup_path"] is not None and (
        not isinstance(result["backup_path"], str) or not re.fullmatch(BACKUP_PATTERN, result["backup_path"])
    ):
        raise ValueError
    return result


def ssh_failure_message(result):
    try:
        error = json.loads(result.stderr)
        if isinstance(error, dict) and error.get("error") in REMOTE_ERRORS:
            return REMOTE_ERRORS[error["error"]]
    except (ValueError, TypeError):
        pass
    return "SSH 连接失败。请先在终端建立可信 SSH 连接，或用 --socket 复用已连接会话；本工具不尝试密码。"


def main(argv=None):
    args = arguments(argv)
    payload = {"action": "configure" if args.configure else "check", "app_id": args.app_id}
    try:
        if args.configure:
            payload = {**payload, "secret": hidden_secret()}
        result = subprocess.run(
            ssh_command(args), input=json.dumps(payload), capture_output=True, text=True, timeout=30
        )
        if result.returncode:
            print(ssh_failure_message(result), file=sys.stderr)
            return 1
        print(json.dumps(sanitized_result(result.stdout)))
        return 0
    except (getpass.GetPassWarning, EOFError, KeyboardInterrupt):
        print("隐藏输入不可用或已取消，请在 Mac 终端重新执行。", file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("SSH 操作超时；未显示请求或错误详情。请先连接服务器，再检查配置状态。", file=sys.stderr)
    except HiddenInputError as error:
        print(INPUT_ERRORS.get(error.args[0], "隐藏输入不可用，请重试。"), file=sys.stderr)
    except ValueError:
        print("服务器返回格式异常；没有显示原始输出或敏感值。", file=sys.stderr)
    except Exception:
        print("操作失败；没有显示敏感错误详情。请检查终端和 SSH 连接。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
