"""
bot.py <-> fleet_bus wiring, for the cases that need NO broker.

Two things are pinned here that the lifecycle suite cannot reach:

  1. `FLEET_BUS_ENABLED=0` runs ZERO NATS code — asserted literally, as
     `"nats" not in sys.modules` after a full boot. That is why fleet_bus
     imports nats-py lazily instead of at module scope: any weaker phrasing
     of "the bus is off" is satisfied by a bus that connects and then throws
     the connection away.

  2. A NATS server that is unreachable is TRANSIENT, not fatal. The bot keeps
     serving Discord and the supervisor keeps retrying, auditing each attempt
     (SPEC §10, "Fleet-bus disconnect — retry with backoff; audit each
     attempt"). This is the difference between "the bus is down" and "the bot
     is down", and it is worth a test because the naive implementation —
     `await bus.run()` in `setup_hook` — deadlocks the whole client instead.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import bot
import fleet_bus
import history

_REPO_ROOT = Path(__file__).resolve().parent.parent

# A port nothing listens on. Chosen high and fixed: the point is a connection
# REFUSED fast, not a hang, so the retry loop spins visibly inside the test.
DEAD_PORT = 45999


# ---------- disabled means disabled ----------


def test_bus_disabled_never_loads_nats_and_starts_no_task():
    """Mutation this exists to catch: `start_bus` ignoring `BUS_CONFIG is
    None` and connecting anyway. The URL points at a closed port, so a bot
    that tried would still boot — which is exactly why the assertion is on
    `sys.modules` and the task handle, not on "did it crash".
    """
    code = (
        "import asyncio, sys, bot\n"
        "async def main():\n"
        "    await bot.bot.setup_hook()\n"
        "    await asyncio.sleep(0.2)\n"
        "    print(json.dumps({\n"
        "        'nats_loaded': 'nats' in sys.modules,\n"
        "        'config': bot.BUS_CONFIG is None,\n"
        "        'task': bot._bus_task is None,\n"
        "        'bus': bot._bus is None,\n"
        "    }))\n"
        "    await bot.bot.close()\n"
        "import json\n"
        "asyncio.run(main())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={
            **os.environ,
            "FLEET_BUS_ENABLED": "0",
            "FLEET_BUS_URL": f"nats://127.0.0.1:{DEAD_PORT}",
        },
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    state = json.loads(result.stdout.strip().splitlines()[-1])
    assert state == {
        "nats_loaded": False,
        "config": True,
        "task": True,
        "bus": True,
    }, state


def test_bus_disabled_leaves_no_audit_log_behind(tmp_path):
    """A disabled bus must not create its audit file either — an empty
    0600 jsonl sitting in a deployment that never opted in is a lie about
    what the process is doing."""
    audit_path = tmp_path / "should-not-exist.jsonl"
    result = subprocess.run(
        [sys.executable, "-c", "import asyncio, bot; asyncio.run(bot.bot.setup_hook())"],
        env={
            **os.environ,
            "FLEET_BUS_ENABLED": "0",
            "FLEET_BUS_AUDIT_LOG": str(audit_path),
        },
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    assert result.returncode == 0, result.stderr
    assert not audit_path.exists()


# ---------- unreachable broker is transient, not fatal ----------


@pytest.fixture
def dead_bus(monkeypatch, tmp_path):
    """Point the adapter at a closed port and start it, the way `setup_hook`
    would. Cleans up whether or not the test got as far as stopping it."""
    audit_path = tmp_path / "audit.jsonl"
    config = fleet_bus.FleetBusConfig(
        bot_name="yugo",
        url=f"nats://127.0.0.1:{DEAD_PORT}",
        user="yugo",
        password="irrelevant",
        allowed_from=frozenset({"yugo"}),
        plugin_version="0.3a-test",
        audit_log_path=str(audit_path),
        heartbeat_interval_s=0.2,
        reconnect_time_wait_s=0.05,
    )
    monkeypatch.setattr(bot, "BUS_CONFIG", config)
    monkeypatch.setattr(bot, "_bus", None)
    monkeypatch.setattr(bot, "_bus_task", None)
    yield audit_path


@pytest.mark.asyncio
async def test_unreachable_broker_audits_every_attempt_and_never_gives_up(dead_bus):
    """SPEC §10: "Fleet-bus disconnect — retry with backoff; audit each
    attempt."

    With `max_reconnect_attempts=-1`, nats-py's own `connect()` is what
    retries — it loops internally and fires `error_cb` per attempt rather than
    raising — so the audit evidence is a growing run of `conn`/`error` lines.
    The supervisor task must still be alive at the end: a bus that gave up
    silently would leave this at a fixed line count with a finished task.
    """
    task = bot.start_bus()
    assert task is not None
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if dead_bus.exists() and len(dead_bus.read_text().splitlines()) >= 3:
                break
            await asyncio.sleep(0.05)
        lines = [json.loads(x) for x in dead_bus.read_text().splitlines()]
        errors = [x for x in lines if x["dir"] == "conn" and x["event"] == "error"]
        assert len(errors) >= 3, f"expected repeated attempt audit; got {lines}"
        assert all("ConnectionRefused" in x["error"] or "Errno 111" in x["error"] for x in errors), (
            f"attempt audit must carry the failure reason; got {errors}"
        )
        # Still retrying, not finished.
        before = len(lines)
        await asyncio.sleep(0.5)
        assert len(dead_bus.read_text().splitlines()) > before
        assert not task.done()
        # And it never got far enough to publish a heartbeat.
        assert not any(x["dir"] == "out" for x in lines)
    finally:
        await bot.stop_bus()


@pytest.mark.asyncio
async def test_discord_path_is_fully_functional_while_the_bus_is_down(
    dead_bus, monkeypatch
):
    """The whole point of "transient": a dead broker costs the fleet lane, not
    the Discord lane.

    Drives a real `bot.on_message` turn — history assembly, LLM seam, send —
    with the supervisor thrashing against a closed port in the same loop.
    """
    history._reset_for_tests()
    seen: list[list[dict]] = []

    async def fake_ask_llm(messages, thread_id):
        seen.append([dict(m) for m in messages])
        return "pong"

    monkeypatch.setattr(bot, "ask_llm", fake_ask_llm)

    task = bot.start_bus()
    try:
        await asyncio.sleep(0.3)  # let the retry loop get going
        channel = _FakeChannel(bot.CHANNEL_ID)
        await bot.on_message(_FakeMessage("ping", channel))
        assert channel.sent == ["pong"]
        assert seen and seen[0][-1] == {"role": "user", "content": "ping"}
        # And the turn was recorded, so the NEXT turn still has context.
        await bot.on_message(_FakeMessage("ping again", channel))
        assert len(seen[1]) == 4, seen[1]
        assert not task.done(), "the bus supervisor must survive a Discord turn"
    finally:
        await bot.stop_bus()
        history._reset_for_tests()


@pytest.mark.asyncio
async def test_stop_bus_from_a_never_connected_supervisor(dead_bus):
    """SIGTERM during the retry loop. There is no client to drain, and
    `stop_bus` must still return without raising."""
    task = bot.start_bus()
    await asyncio.sleep(0.2)
    await asyncio.wait_for(bot.stop_bus(), timeout=10)
    assert task.done()
    assert bot._bus_task is None
    assert bot._bus is None


@pytest.mark.asyncio
async def test_start_bus_twice_against_a_dead_broker_is_still_one_task(dead_bus):
    """Idempotency must not depend on the connect succeeding — `setup_hook`
    could be re-entered while the first supervisor is still retrying."""
    first = bot.start_bus()
    second = bot.start_bus()
    try:
        assert first is second
        assert not first.done()
    finally:
        await bot.stop_bus()


# ---------- placement ----------


def test_setup_hook_starts_the_bus_and_on_ready_does_not():
    """`on_ready` re-fires on every gateway resume — that is what put `_lock`
    at module scope (see bot.py). Starting NATS there would give a second
    client, duplicate heartbeats on `fleet.<self>.status` and a doubled audit
    trail on the first resume, so the start site is part of the contract.
    """
    import inspect

    setup_hook_src = inspect.getsource(bot.YugoBot.setup_hook)
    assert "start_bus()" in setup_hook_src
    assert "start_bus" not in inspect.getsource(bot.on_ready)
    assert "fleet_bus" not in inspect.getsource(bot.on_ready)


def test_close_stops_the_bus_before_closing_the_gateway():
    import inspect

    src = inspect.getsource(bot.YugoBot.close)
    assert src.index("stop_bus") < src.index("super().close"), (
        "drain the bus before tearing down the Discord client, so a shutdown "
        "in-flight heartbeat still has a loop to run on"
    )


# ---------- minimal Discord fakes (mirrors test_bot_messages.py) ----------


class _FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.sent: list[str] = []

    def typing(self):
        return _FakeTyping()

    async def send(self, content):
        self.sent.append(content)


class _FakeAuthor:
    bot = False


class _FakeMessage:
    def __init__(self, content, channel):
        self.content = content
        self.channel = channel
        self.author = _FakeAuthor()
