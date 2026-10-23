"""Shared test setup: make `triage` importable and keep every test offline.

The demo folder has a hyphen in its name, so it is not a package. We put it on
sys.path instead, and there is deliberately no __init__.py in this tests folder.
"""
import shlex
import socket
import subprocess
import sys
from pathlib import Path

import pytest

DEMO_DIR = Path(__file__).resolve().parents[1]
if str(DEMO_DIR) not in sys.path:
    sys.path.insert(0, str(DEMO_DIR))

LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _program_name(args) -> str:
    """Basename of the program a subprocess call would start."""
    if isinstance(args, bytes):
        args = args.decode()
    parts = shlex.split(args) if isinstance(args, str) else [str(a) for a in args]
    return Path(parts[0]).name if parts else ""


@pytest.fixture(autouse=True)
def offline_guard(monkeypatch):
    """Fail any test that reaches the network or starts the `claude` CLI."""
    real_connect = socket.socket.connect
    real_popen_init = subprocess.Popen.__init__

    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else None
        if self.family != socket.AF_UNIX and host not in LOCAL_HOSTS:
            raise RuntimeError(f"tests may not connect to {address!r}")
        return real_connect(self, address, *args, **kwargs)

    def guarded_popen_init(self, args, *pargs, **kwargs):
        if _program_name(args) == "claude":
            raise RuntimeError("tests may not start the claude CLI")
        return real_popen_init(self, args, *pargs, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(subprocess.Popen, "__init__", guarded_popen_init)
