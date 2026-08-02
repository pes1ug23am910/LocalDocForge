#!/usr/bin/env python3
"""Run a command with Python DNS and non-loopback sockets denied.

The injected sitecustomize module is inherited by spawned Python interpreters,
so this gate covers spawned workers as well as the pytest parent process.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD_DIR = Path(__file__).resolve().parent / "_blocked_network"
PROBE = """
import socket

socket.getaddrinfo("localhost", 0)
socket.getaddrinfo("127.0.0.1", 0)
for operation in (
    lambda: socket.getaddrinfo("example.invalid", 443),
    lambda: socket.create_connection(("198.51.100.1", 443), timeout=0.01),
):
    try:
        operation()
    except OSError as error:
        if "blocked-network gate denied" not in str(error):
            raise
    else:
        raise SystemExit("blocked-network startup guard was not active")
"""


def _guarded_environment() -> dict[str, str]:
    environment = os.environ.copy()
    existing = environment.get("PYTHONPATH")
    paths = [str(GUARD_DIR)]
    if existing:
        paths.append(existing)
    environment["PYTHONPATH"] = os.pathsep.join(paths)
    environment["LDF_BLOCK_NETWORK"] = "1"
    environment["LDF_BLOCK_NETWORK_GUARD_DIR"] = str(GUARD_DIR.resolve())
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="command to run after --; defaults to the full pytest suite",
    )
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        command = [sys.executable, "-m", "pytest", "tests", "-q"]

    environment = _guarded_environment()
    subprocess.run(  # noqa: S603
        [sys.executable, "-c", PROBE],
        cwd=ROOT,
        env=environment,
        check=True,
    )
    completed = subprocess.run(command, cwd=ROOT, env=environment, check=False)  # noqa: S603
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
