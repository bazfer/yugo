"""Coordinator core-NATS heartbeat and discriminating tap outage alerts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import nats
import pytest
from fleet_bus import create_heartbeat_envelope

from coordinator import (
    COORDINATOR_STATUS_SUBJECT,
    CoordinatorHeartbeat,
    CoordinatorNatsError,
    TapHeartbeatMonitor,
    load_heartbeat_ms,
)
from test.nats_server import NatsServer, _free_port, nats_server_binary


def test_heartbeat_interval_defaults_and_fails_closed():
    assert load_heartbeat_ms({}) == 5_000
    assert load_heartbeat_ms({"COORDINATOR_HEARTBEAT_MS": "250"}) == 250
    for value in ("0", "-1", "not-a-number"):
        with pytest.raises(CoordinatorNatsError):
            load_heartbeat_ms({"COORDINATOR_HEARTBEAT_MS": value})


@pytest.mark.asyncio
async def test_heartbeat_is_core_nats_and_never_enters_a_stream(tmp_path):
    binary = nats_server_binary()
    if binary is None:
        if __import__("os").environ.get("CI"):
            pytest.fail("CI must run the broker-backed heartbeat test")
        pytest.skip("no nats-server binary")

    port = _free_port()
    source = (Path(__file__).resolve().parent.parent / "config/nats-coordinator-authz.conf").read_text()
    source = source.replace("CHANGEME_COORDINATOR", "coord-test")
    source = source.replace("CHANGEME_BOT", "bot-test")
    source = source.replace("CHANGEME_CONSOLE", "console-test")
    source = source.replace("# TEST_PROVISIONER", '{user: provisioner, password: "provision-test"}')
    config = tmp_path / "nats.conf"
    config.write_text(f'port: {port}\njetstream {{ store_dir: "{tmp_path / "js"}" }}\n{source}')
    server = NatsServer(binary, config, port)
    server.start()
    provisioner = coordinator = console = None
    try:
        provisioner = await nats.connect(server.url, user="provisioner", password="provision-test")
        js = provisioner.jetstream()
        await js.add_stream(name="FLEET_REQUEST", subjects=["fleet.*.request"])
        await js.add_stream(name="FLEET_INBOX", subjects=["fleet.*.inbox"])

        seen = asyncio.Future()
        console = await nats.connect(server.url, user="console", password="console-test")

        async def on_status(message):
            if not seen.done():
                seen.set_result(message)

        await console.subscribe(COORDINATOR_STATUS_SUBJECT, cb=on_status)
        await console.flush()
        coordinator = await nats.connect(server.url, user="coordinator", password="coord-test")
        envelope = await CoordinatorHeartbeat(coordinator, version="test").emit_once()
        message = await asyncio.wait_for(seen, timeout=2)

        assert message.subject == COORDINATOR_STATUS_SUBJECT
        assert json.loads(message.data) == envelope
        assert envelope["from"] == "coordinator"
        assert envelope["kind"] == "status_heartbeat"
        assert (await js.stream_info("FLEET_REQUEST")).state.messages == 0
        assert (await js.stream_info("FLEET_INBOX")).state.messages == 0
    finally:
        for client in (console, coordinator, provisioner):
            if client is not None:
                await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_heartbeat_uses_core_publish_api_not_jetstream():
    calls = []

    class CoreConnection:
        def jetstream(self):
            pytest.fail("heartbeat must never acquire the JetStream API")

        async def publish(self, subject, data):
            calls.append((subject, data))

        async def flush(self):
            calls.append(("flush",))

    await CoordinatorHeartbeat(CoreConnection(), version="test").emit_once()
    assert calls[0][0] == COORDINATOR_STATUS_SUBJECT
    assert calls[1] == ("flush",)


@pytest.mark.asyncio
async def test_silence_over_three_intervals_alerts_both_channels_once():
    now = [100.0]
    alerts = []
    failures = []

    def alert(channel, event):
        alerts.append((channel, event))
        if channel == "coord-channel":
            failures.append(channel)
            raise RuntimeError("Discord channel rate limited")

    monitor = TapHeartbeatMonitor(
        heartbeat_ms=5_000,
        coordinator_channel_id="coord-channel",
        paging_channel_id="page-channel",
        alert=alert,
        clock=lambda: now[0],
        logger=lambda _line: None,
    )

    now[0] += 15.0
    assert not await monitor.check(), "threshold is strictly greater than 3x"
    now[0] += 0.001
    assert await monitor.check()
    assert [channel for channel, _ in alerts] == ["coord-channel", "page-channel"]
    assert failures == ["coord-channel"]
    assert all(event["kind"] == "coordinator_heartbeat_silent" for _, event in alerts)
    assert not await monitor.check(), "one incident must not page every poll"

    # A valid beat recovers the incident and permits a later outage alert.
    heartbeat = type("Message", (), {
        "subject": COORDINATOR_STATUS_SUBJECT,
        "data": json.dumps(create_heartbeat_envelope("coordinator", "test")).encode(),
    })()
    assert await monitor.observe(heartbeat)
    now[0] += 15.001
    assert await monitor.check()
    assert len(alerts) == 4


@pytest.mark.asyncio
async def test_heartbeat_loop_emits_again_after_a_publish_failure():
    second_iteration = asyncio.Event()

    class Heartbeat(CoordinatorHeartbeat):
        def __init__(self):
            super().__init__(object(), version="test", interval_ms=1, logger=lambda _line: None)
            self.calls = 0

        async def emit_once(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("broker blip")
            second_iteration.set()
            return {}

    heartbeat = Heartbeat()
    task = asyncio.create_task(heartbeat.run())
    try:
        await asyncio.wait_for(second_iteration.wait(), timeout=1)
        assert heartbeat.calls >= 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_tap_monitor_checks_again_after_check_failure():
    second_check = asyncio.Event()

    class Connection:
        async def subscribe(self, _subject, *, cb):
            self.callback = cb

        async def flush(self):
            return None

    class Monitor(TapHeartbeatMonitor):
        def __init__(self):
            super().__init__(
                heartbeat_ms=1,
                coordinator_channel_id="coord-channel",
                alert=lambda _channel, _event: None,
                connection=Connection(),
                logger=lambda _line: None,
            )
            self.calls = 0

        async def check(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("monitor dependency failed")
            second_check.set()
            return False

    monitor = Monitor()
    task = asyncio.create_task(monitor.run())
    try:
        await asyncio.wait_for(second_check.wait(), timeout=1)
        assert monitor.calls >= 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_duplicate_forwards_are_not_an_alert_signal():
    now = [0.0]
    alerts = []
    monitor = TapHeartbeatMonitor(
        heartbeat_ms=5_000,
        coordinator_channel_id="coord-channel",
        alert=lambda channel, event: alerts.append((channel, event)),
        clock=lambda: now[0],
    )
    duplicate = type("Message", (), {
        "subject": "fleet.vec.inbox",
        "data": b'{"id":"same-envelope"}',
    })()
    now[0] = 5.0
    for _ in range(2):
        assert not await monitor.observe(duplicate)
    malformed = type("Message", (), {
        "subject": COORDINATOR_STATUS_SUBJECT,
        "data": b"not-json",
    })()
    now[0] = 10.0
    assert not await monitor.observe(malformed)
    wrong_kind_envelope = create_heartbeat_envelope("coordinator", "test")
    wrong_kind_envelope["kind"] = "forwarded_envelope"
    wrong_kind = type("Message", (), {
        "subject": COORDINATOR_STATUS_SUBJECT,
        "data": json.dumps(wrong_kind_envelope).encode(),
    })()
    now[0] = 14.0
    assert not await monitor.observe(wrong_kind)
    assert alerts == []
    now[0] = 15.001
    assert await monitor.check()
    assert len(alerts) == 1  # silence alerts; duplicate delivery did not


@pytest.mark.asyncio
async def test_inflight_old_alert_cannot_relatch_a_recovered_incident():
    now = [0.0]
    release = asyncio.Event()
    delivered = []

    async def blocking_alert(_channel, event):
        delivered.append(event)
        await release.wait()

    monitor = TapHeartbeatMonitor(
        heartbeat_ms=5_000,
        coordinator_channel_id="coord-channel",
        alert=blocking_alert,
        clock=lambda: now[0],
        logger=lambda _line: None,
    )
    now[0] = 15.001
    old_alert = asyncio.create_task(monitor.check())
    await asyncio.sleep(0)
    assert len(delivered) == 1

    now[0] = 16.0
    heartbeat = type("Message", (), {
        "subject": COORDINATOR_STATUS_SUBJECT,
        "data": json.dumps(create_heartbeat_envelope("coordinator", "test")).encode(),
    })()
    assert await monitor.observe(heartbeat)
    release.set()
    assert await old_alert

    now[0] = 31.001
    assert await monitor.check()
    assert len(delivered) == 2
