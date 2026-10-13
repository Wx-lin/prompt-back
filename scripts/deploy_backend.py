#!/usr/bin/env python3
"""Deploy the existing Tencent Cloud backend. Requires Python 3, SSH and project venv."""

import argparse
import hashlib
import shlex
import subprocess
import tarfile
import tempfile
from datetime import datetime
from pathlib import Path

SOURCE_DIRS = ("app", "scripts", "alembic", "deploy")
SOURCE_FILES = ("alembic.ini", "pyproject.toml", "uv.lock")


def package_release(root, output):
    sources = [root / name for name in (*SOURCE_DIRS, *SOURCE_FILES)]
    for source in sources:
        if not source.exists():
            raise FileNotFoundError(source)
    with tarfile.open(output, "w:gz") as archive:
        for source in sources:
            paths = [source, *sorted(source.rglob("*"))] if source.is_dir() else [source]
            for path in paths:
                relative = path.relative_to(root)
                if any(part.startswith(".env") or part == "__pycache__" for part in relative.parts):
                    continue
                if path.is_symlink():
                    raise ValueError(f"Release source must not be a symlink: {relative}")
                if path.suffix == ".pyc":
                    continue
                archive.add(path, arcname=f"backend/{relative}", recursive=False)


def run(args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="ubuntu@62.234.69.93")
    parser.add_argument("--socket", help="Reuse an existing SSH control socket")
    parser.add_argument("--dry-run", action="store_true", help="Validate and package locally only")
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="Apply reviewed backward-compatible migrations with the new image before switching",
    )
    args = parser.parse_args()
    if args.host.startswith("-") or any(c.isspace() for c in args.host):
        parser.error("Invalid SSH host")
    root = Path(__file__).resolve().parents[1]
    python = root / ".venv/bin/python"
    run([str(root / ".venv/bin/ruff"), "check", "app", "scripts", "tests"], cwd=root)
    run([str(python), "-m", "pytest", "-q"], cwd=root)
    release = datetime.now().strftime("backend-%Y%m%d-%H%M%S")
    with tempfile.TemporaryDirectory(prefix="aigc-deploy-") as temp:
        archive = Path(temp) / "release.tar.gz"
        package_release(root, archive)
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        print(f"Release: {release}; SHA256: {digest}", flush=True)
        if args.dry_run:
            print("Local checks passed. No server changes.")
            return
        socket = args.socket or str(Path(temp) / "control")
        ssh = ["ssh", "-S", socket, "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10"]
        owned = not args.socket
        try:
            if owned:
                run([*ssh, "-M", "-o", "ControlPersist=1800", args.host, "hostname && whoami"])
            run(
                [
                    *ssh,
                    "-o",
                    "BatchMode=yes",
                    args.host,
                    f"umask 077; mkdir -p /opt/aigc-prompt/releases/{release}",
                ]
            )
            run(
                [
                    "scp",
                    "-o",
                    f"ControlPath={socket}",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    str(archive),
                    f"{args.host}:/opt/aigc-prompt/releases/{release}/release.tar.gz",
                ]
            )
            run(
                [
                    "scp",
                    "-o",
                    f"ControlPath={socket}",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    str(root / "deploy/update-backend.sh"),
                    f"{args.host}:/opt/aigc-prompt/releases/{release}/update-backend.sh",
                ]
            )
            command = "bash " + shlex.quote(f"/opt/aigc-prompt/releases/{release}/update-backend.sh")
            command += " " + " ".join(
                map(shlex.quote, (release, digest, "true" if args.migrate else "false"))
            )
            run([*ssh, "-o", "BatchMode=yes", args.host, command])
            run(
                [
                    "curl",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--retry",
                    "3",
                    "https://62.234.69.93:8443/health",
                ]
            )
            print(f"\nDeployment successful. Backup: /opt/aigc-prompt/releases/{release}")
        finally:
            if owned:
                subprocess.run([*ssh, "-O", "exit", args.host], capture_output=True)


if __name__ == "__main__":
    try:
        main()
    except (subprocess.CalledProcessError, OSError, ValueError) as error:
        raise SystemExit(f"Deployment stopped: {error}") from error
