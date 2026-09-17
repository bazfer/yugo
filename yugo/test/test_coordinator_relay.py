"""Broker-backed controls for the v0.6a durable pull-and-forward loop."""

from __future__ import annotations

import asyncio
from pathlib import Path

import nats
import pytest
from nats.js.api import DeliverPolicy, DiscardPolicy, StreamConfig

from coordinator import CoordinatorRelay, REQUEST_DURABLE, TASK_ERROR_DELAY_S
from test.nats_server import NatsServer, _free_port, nats_server_binary


@pytest.mark.asyncio
async def test_request_is_forwarded_to_recipient_inbox_and_acked(tmp_path):
    """Mutation control: removing/disabling the relay makes inbox_seen time out."""
    binary = nats_server_binary()
    if binary is None:
        if __import__("os").environ.get("CI"):
            pytest.fail("CI must run the broker-backed coordinator relay test")
        pytest.skip("no nats-server binary")

    port = _free_port()
    source = (Path(__file__).resolve().parent.parent / "config/nats-coordinator-authz.conf").read_text()
    source = source.replace("CHANGEME_COORDINATOR", "coord-test")
    source = source.replace("CHANGEME_BOT", "bot-test")
    source = source.replace("CHANGEME_CONSOLE", "console-test")
    source = source.replace("# TEST_PROVISIONER", '{user: provisioner, password: "provision-test"}')
    config_path = tmp_path / "nats.conf"
    config_path.write_text(f'port: {port}\njetstream {{ store_dir: "{tmp_path / "js"}" }}\n{source}')
    server = NatsServer(binary, config_path, port)
    server.start()
    provisioner = coordinator = bot = None
    relay_task = None
    try:
        provisioner = await nats.connect(server.url, user="provisioner", password="provision-test")
        admin_js = provisioner.jetstream()
        seven_days = 7 * 24 * 60 * 60
        for name, subject in (
            ("FLEET_REQUEST", "fleet.*.request"),
            ("FLEET_INBOX", "fleet.*.inbox"),
        ):
            await admin_js.add_stream(config=StreamConfig(
                name=name,
                subjects=[subject],
                max_age=seven_days,
                max_msgs_per_subject=100_000,
                discard=DiscardPolicy.OLD,
            ))

        coordinator = await nats.connect(server.url, user="coordinator", password="coord-test")
        relay = CoordinatorRelay(coordinator)
        await relay.open()  # DeliverPolicy.NEW must be established before the probe.

        inbox_seen = asyncio.Event()
        forwarded = []

        async def on_inbox(message):
            forwarded.append(message.data)
            inbox_seen.set()

        await coordinator.subscribe("fleet.vec.inbox", cb=on_inbox)
        await coordinator.flush()
        relay_task = asyncio.create_task(relay.run())

        bot = await nats.connect(server.url, user="fleet-bot", password="bot-test")
        envelope = b'{"id":"relay-probe","to":"vec"}'
        await bot.publish("fleet.vec.request", envelope)
        await bot.flush()

        await asyncio.wait_for(inbox_seen.wait(), timeout=2)
        assert forwarded == [envelope]
        inbox_info = await admin_js.stream_info("FLEET_INBOX")
        assert inbox_info.state.messages == 1

        deadline = asyncio.get_running_loop().time() + 2
        while True:
            consumer = await admin_js.consumer_info("FLEET_REQUEST", REQUEST_DURABLE)
            if consumer.num_ack_pending == 0 and consumer.ack_floor.stream_seq == 1:
                break
            if asyncio.get_running_loop().time() >= deadline:
                pytest.fail("request remained unacked after inbox PubAck")
            await asyncio.sleep(0.01)
        assert consumer.config.deliver_policy == DeliverPolicy.NEW

        # The streams use limits retention: ack state does not extend max_age.
        request_info = await admin_js.stream_info("FLEET_REQUEST")
        assert request_info.config.max_age == seven_days
        assert request_info.config.max_age > 15 * 60
    finally:
        if relay_task is not None:
            relay_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await relay_task
        for client in (bot, coordinator, provisioner):
            if client is not None:
                await client.close()
        server.stop()


@pytest.mark.asyncio
async def test_transient_publish_failure_naks_with_delay():
    class JetStream:
        async def publish(self, _subject, _data):
            raise TimeoutError("transient")

    class Connection:
        def jetstream(self):
            return JetStream()

    class Message:
        subject = "fleet.vec.request"
        data = b"envelope"

        def __init__(self):
            self.nak_delay = None
            self.acked = False

        async def nak(self, *, delay):
            self.nak_delay = delay

        async def ack(self):
            self.acked = True

    message = Message()
    await CoordinatorRelay(Connection(), nak_delay_s=3.5, logger=lambda _line: None).forward(message)
    assert message.nak_delay == 3.5
    assert not message.acked


@pytest.mark.asyncio
async def test_successful_puback_precedes_request_ack():
    events = []

    class JetStream:
        async def publish(self, subject, data):
            events.append(("publish", subject, data))

    class Connection:
        def jetstream(self):
            return JetStream()

    class Message:
        subject = "fleet.vec.request"
        data = b"unchanged-envelope"

        async def ack(self):
            events.append(("ack",))

    await CoordinatorRelay(Connection()).forward(Message())
    assert events == [
        ("publish", "fleet.vec.inbox", b"unchanged-envelope"),
        ("ack",),
    ]


@pytest.mark.asyncio
async def test_relay_fetches_again_after_connection_error(monkeypatch):
    second_fetch = asyncio.Event()
    error_sleeps = []

    async def fake_sleep(delay):
        error_sleeps.append(delay)

    monkeypatch.setattr("coordinator.asyncio.sleep", fake_sleep)

    class Subscription:
        def __init__(self):
            self.calls = 0

        async def fetch(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("reconnecting")
            second_fetch.set()
            await asyncio.Future()

    class Connection:
        def jetstream(self):
            return object()

    relay = CoordinatorRelay(Connection(), logger=lambda _line: None)
    relay._subscription = Subscription()
    task = asyncio.create_task(relay.run())
    try:
        await asyncio.wait_for(second_fetch.wait(), timeout=1)
        assert relay.subscription.calls >= 2
        assert TASK_ERROR_DELAY_S in error_sleeps
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_relay_processes_next_delivery_after_ack_failure():
    second_acked = asyncio.Event()

    class JetStream:
        async def publish(self, _subject, _data):
            return None

    class Message:
        subject = "fleet.vec.request"
        data = b"envelope"

        def __init__(self, fail=False):
            self.fail = fail

        async def ack(self):
            if self.fail:
                raise ConnectionError("ack connection lost")
            second_acked.set()

    class Subscription:
        def __init__(self):
            self.calls = 0

        async def fetch(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return [Message(fail=True)]
            if self.calls == 2:
                return [Message()]
            await asyncio.Future()

    class Connection:
        def jetstream(self):
            return JetStream()

    relay = CoordinatorRelay(Connection(), logger=lambda _line: None)
    relay._subscription = Subscription()
    task = asyncio.create_task(relay.run())
    try:
        await asyncio.wait_for(second_acked.wait(), timeout=1)
        assert relay.subscription.calls >= 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["fetch", "ack"])
async def test_closed_connection_exits_relay_loudly(site):
    from nats.errors import ConnectionClosedError

    class JetStream:
        async def publish(self, _subject, _data):
            return None

    class Message:
        subject = "fleet.vec.request"
        data = b"envelope"

        async def ack(self):
            raise ConnectionClosedError

    class Subscription:
        async def fetch(self, **_kwargs):
            if site == "fetch":
                raise ConnectionClosedError
            return [Message()]

    class Connection:
        def jetstream(self):
            return JetStream()

    logs = []
    relay = CoordinatorRelay(Connection(), logger=logs.append)
    relay._subscription = Subscription()
    with pytest.raises(ConnectionClosedError):
        await asyncio.wait_for(relay.run(), timeout=0.2)
    assert any("terminal" in line for line in logs)


@pytest.mark.asyncio
async def test_relay_processes_next_delivery_after_delayed_nak_failure():
    second_acked = asyncio.Event()

    class JetStream:
        def __init__(self):
            self.calls = 0

        async def publish(self, _subject, _data):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("inbox publish failed")

    class Message:
        subject = "fleet.vec.request"
        data = b"envelope"

        def __init__(self, nak_fails=False):
            self.nak_fails = nak_fails

        async def nak(self, *, delay):
            assert delay > 0
            if self.nak_fails:
                raise RuntimeError("nak transport failed")

        async def ack(self):
            second_acked.set()

    class Subscription:
        def __init__(self):
            self.calls = 0

        async def fetch(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return [Message(nak_fails=True)]
            if self.calls == 2:
                return [Message()]
            await asyncio.Future()

    js = JetStream()

    class Connection:
        def jetstream(self):
            return js

    relay = CoordinatorRelay(Connection(), logger=lambda _line: None)
    relay._subscription = Subscription()
    task = asyncio.create_task(relay.run())
    try:
        await asyncio.wait_for(second_acked.wait(), timeout=1)
        assert relay.subscription.calls >= 2
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
