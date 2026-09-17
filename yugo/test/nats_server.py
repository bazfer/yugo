"""
Throwaway nats-server harness for the fleet-bus lifecycle tests.

Why a REAL server and not a fake connection: every invariant slice 3a exists
to prove — reconnect-without-close, library-side subscription replay,
heartbeat loopback through the broker, drain-vs-close on shutdown — lives in
nats-py's connection state machine and the broker's SUB bookkeeping. A fake
`connect` would let us assert our own mock back at ourselves and prove
nothing about any of it.

The server is started on a free port with a user/password the adapter
authenticates against, and can be stopped and restarted ON THE SAME PORT so a
test can simulate an outage.

Skips when no `nats-server` binary is available, so a laptop without one still
gets a green pure-unit run — but FAILS instead of skipping under `$CI`. A
silently-skipped lifecycle suite is indistinguishable from a passing one in
pytest's summary line, and it is the only place the reconnect invariants are
proved at all. CI installs the binary; see .github/workflows/ci.yml.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

BOT_NAME = "yugo"
BOT_PASSWORD = "s3cret-test"
PEER_NAME = "vec"
PEER_PASSWORD = "peer-test"


def nats_server_binary() -> str | None:
    override = os.environ.get("NATS_SERVER_BIN", "").strip()
    if override:
        return override if Path(override).is_file() else None
    return shutil.which("nats-server")


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class NatsServer:
    """A nats-server process on a fixed port, stoppable and restartable."""

    def __init__(self, binary: str, config_path: Path, port: int) -> None:
        self._binary = binary
        self._config_path = config_path
        self.port = port
        self._proc: subprocess.Popen | None = None

    @property
    def url(self) -> str:
        return f"nats://127.0.0.1:{self.port}"

    @property
    def running(self) -> bool:
        return self._proc is not None

    def start(self, timeout_s: float = 10.0) -> None:
        assert self._proc is None, "server already running"
        self._proc = subprocess.Popen(
            [self._binary, "-c", str(self._config_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                return
            except OSError:
                time.sleep(0.05)
        raise RuntimeError(f"nats-server did not accept connections on {self.port}")

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(10)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(10)
        self._proc = None


@pytest.fixture
def nats_server(tmp_path):
    binary = nats_server_binary()
    if binary is None:
        message = "no nats-server binary (set NATS_SERVER_BIN or put it on PATH)"
        if os.environ.get("CI"):
            pytest.fail(f"{message} — CI must run the broker-backed suite, not skip it")
        pytest.skip(message)
    port = _free_port()
    config = tmp_path / "nats.conf"
    config.write_text(
        f"port: {port}\n"
        "authorization {\n"
        "  users = [\n"
        f'    {{user: {BOT_NAME}, password: "{BOT_PASSWORD}"}},\n'
        f'    {{user: {PEER_NAME}, password: "{PEER_PASSWORD}"}}\n'
        "  ]\n"
        "}\n",
        encoding="utf-8",
    )
    server = NatsServer(binary, config, port)
    server.start()
    try:
        yield server
    finally:
        server.stop()
