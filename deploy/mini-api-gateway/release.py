#!/usr/bin/env python3
"""Gateway-only release. Run from a reviewed Git artifact as root; never rebuild apps."""

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = Path("/opt/aigc-prompt")
TARGET = ROOT / "backend/deploy/nginx.conf"
CONTAINER = "aigc-prompt-gateway-1"
HTTP_BEGIN = "# BEGIN REVIEWED MINI API HTTP\n"
HTTP_END = "# END REVIEWED MINI API HTTP\n"
SERVER_BEGIN = "\n# BEGIN REVIEWED MINI API SERVER\n"
SERVER_END = "# END REVIEWED MINI API SERVER\n"

BROWSE_HTTP_SHA256 = "433bd3efc3f037adf42942f2dbd42cfa6691e68517c6aea9347088ad38d3d949"
BROWSE_SERVER_SHA256 = "6045aca9faa99451ec5500f03dfb6d353a533e475be0e28b02857f011882d452"
AUTH_HTTP_SHA256 = "cec0167efdc846f22b080f3dc943f67379485f8c2605de66ba3c661ddf6ade3c"
AUTH_SERVER_SHA256 = "6045aca9faa99451ec5500f03dfb6d353a533e475be0e28b02857f011882d452"
PROFILE_HTTP_SHA256 = "c84e6888566667714acd86ca702a8ca9a5c7842d2fa46cd5d2c40eaefac26b97"
PROFILE_SERVER_SHA256 = "0fe8a6f52e052eb4e6e688471f9b76161ae0d84ecdee81033f494e56b213bbab"
REVIEWED_PREVIOUS_FRAGMENTS = frozenset(
    {
        (BROWSE_HTTP_SHA256, BROWSE_SERVER_SHA256),
        (AUTH_HTTP_SHA256, AUTH_SERVER_SHA256),
        (PROFILE_HTTP_SHA256, PROFILE_SERVER_SHA256),
    }
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def mask(text):
    """Preserve offsets while hiding comments and quoted strings from brace parser."""
    return re.sub(
        r"""\#[^\n]*|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*' """, lambda m: " " * len(m.group()), text, flags=re.X
    )


def domain_block(text):
    masked = mask(text)
    matches = []
    for match in re.finditer(r"\bserver\s*\{", masked):
        depth = 1
        index = match.end()
        while index < len(masked) and depth:
            depth += (masked[index] == "{") - (masked[index] == "}")
            index += 1
        if depth:
            raise ValueError("Unbalanced server block")
        block = masked[match.end() : index - 1]
        names = re.findall(r"\bserver_name\s+([^;]+);", block)
        if any("aigc-prompt.com" in namestring.split() for namestring in names):
            matches.append((match.start(), index - 1))
    if len(matches) != 1:
        raise ValueError("Expected exactly one aigc-prompt.com server")
    return matches[0]


def merge(original):
    if "mini-api" in original or "$mini_api_allowed" in original:
        base = remove(original)
        http_start = original.index(HTTP_BEGIN) + len(HTTP_BEGIN)
        server_start = original.index(SERVER_BEGIN) + len(SERVER_BEGIN)
        previous_http = original[http_start : original.index(HTTP_END)]
        previous_server = original[server_start : original.index(SERVER_END)]
        previous_hashes = (digest(previous_http.encode()), digest(previous_server.encode()))
        if previous_hashes not in REVIEWED_PREVIOUS_FRAGMENTS:
            raise ValueError("Only a reviewed browse, auth or profile gateway can be upgraded")
        _, base_end = domain_block(base)
        expected = (
            HTTP_BEGIN
            + previous_http
            + HTTP_END
            + base[:base_end]
            + SERVER_BEGIN
            + previous_server
            + SERVER_END
            + base[base_end:]
        )
        if expected != original:
            raise ValueError("Existing gateway markers are outside expected scopes")
        return merge(base)
    _, end = domain_block(original)
    http = HTTP_BEGIN + (BASE / "http.conf").read_text() + HTTP_END
    server = SERVER_BEGIN + (BASE / "server.conf").read_text() + SERVER_END
    candidate = http + original[:end] + server + original[end:]
    if remove(candidate) != original:
        raise ValueError("Unexpected merge changes")
    return candidate


def remove(candidate):
    text = candidate
    for begin, end in [(HTTP_BEGIN, HTTP_END), (SERVER_BEGIN, SERVER_END)]:
        if text.count(begin) != 1 or text.count(end) != 1:
            raise ValueError("Missing or repeated gateway markers")
        start = text.index(begin)
        finish = text.index(end, start) + len(end)
        text = text[:start] + text[finish:]
    return text


def write_in_place(data):
    # Retain inode/ownership/mode: nginx binds this exact file, not its directory.
    with TARGET.open("r+b") as stream:
        stream.write(data)
        stream.truncate()
        stream.flush()
        os.fsync(stream.fileno())


def nginx(action):
    subprocess.run(["docker", "exec", CONTAINER, "nginx", action], check=True, timeout=25)


def reload_nginx():
    subprocess.run(["docker", "exec", CONTAINER, "nginx", "-s", "reload"], check=True, timeout=25)


def verify_mount(expected):
    result = subprocess.run(
        ["docker", "exec", CONTAINER, "sha256sum", "/etc/nginx/conf.d/default.conf"],
        check=True,
        timeout=25,
        capture_output=True,
        text=True,
    )
    if result.stdout.split()[0] != expected:
        raise ValueError("Container nginx bind mount differs from host; refusing reload-only release")


def install(candidate, original, expected):
    if digest(TARGET.read_bytes()) != expected:
        raise ValueError("Concurrent nginx configuration change; refusing overwrite")
    verify_mount(expected)
    inode = TARGET.stat().st_ino
    try:
        write_in_place(candidate)
        if TARGET.stat().st_ino != inode:
            raise ValueError("Target inode changed")
        verify_mount(digest(candidate))
        nginx("-t")
        reload_nginx()
        verify_mount(digest(candidate))
    except BaseException:
        write_in_place(original)
        verify_mount(digest(original))
        nginx("-t")
        reload_nginx()
        raise


def apply(expected, sha):
    original = TARGET.read_bytes()
    if digest(original) != expected:
        raise ValueError("Production hash differs from reviewed hash")
    candidate = merge(original.decode()).encode()
    name = datetime.datetime.now(datetime.UTC).strftime("mini-api-%Y%m%d-%H%M%S-") + str(os.getpid())
    backup = ROOT / "releases" / name
    backup.mkdir(mode=0o700)
    for filename, data in [("original.conf", original), ("candidate.conf", candidate)]:
        path = backup / filename
        path.write_bytes(data)
        path.chmod(0o600)
    metadata = {"git_sha": sha, "original_sha256": digest(original), "candidate_sha256": digest(candidate)}
    (backup / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (backup / "manifest.json").chmod(0o600)
    install(candidate, original, expected)
    print(json.dumps({"status": "reloaded", "backup": str(backup), **metadata}))


def rollback(backup):
    path = Path(backup).resolve()
    if path.parent != (ROOT / "releases").resolve() or not path.name.startswith("mini-api-"):
        raise ValueError("Invalid backup location")
    metadata = json.loads((path / "manifest.json").read_text())
    original = (path / "original.conf").read_bytes()
    candidate = (path / "candidate.conf").read_bytes()
    if digest(original) != metadata["original_sha256"] or digest(candidate) != metadata["candidate_sha256"]:
        raise ValueError("Backup integrity check failed")
    install(original, candidate, metadata["candidate_sha256"])
    print(json.dumps({"status": "rolled_back", "backup": str(path)}))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("source")
    plan.add_argument("output")
    release = sub.add_parser("apply")
    release.add_argument("--expected-sha256", required=True)
    release.add_argument("--git-sha", required=True)
    undo = sub.add_parser("rollback")
    undo.add_argument("backup")
    args = parser.parse_args()
    if args.action == "plan":
        Path(args.output).write_text(merge(Path(args.source).read_text()))
        return
    if os.geteuid() != 0:
        raise ValueError("Release requires root")
    os.umask(0o077)
    with (ROOT / ".release-deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == "apply":
            if not re.fullmatch(r"[0-9a-f]{64}", args.expected_sha256) or not re.fullmatch(
                r"[0-9a-f]{40}", args.git_sha
            ):
                raise ValueError("Invalid source SHA/hash")
            apply(args.expected_sha256, args.git_sha)
        else:
            rollback(args.backup)


if __name__ == "__main__":
    main()
