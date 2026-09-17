"""Coordinator NATS identity and the fleet inbox ownership boundary."""

from __future__ import annotations

import asyncio
from pathlib import Path

import nats
import pytest

from coordinator import (
    COORDINATOR_NATS_USER,
    CoordinatorNatsError,
    connect_nats,
    load_nats_config,
)
from test.nats_server import NatsServer, _free_port, nats_server_binary


def test_config_requires_the_dedicated_coordinator_identity(tmp_path):
    token = tmp_path / "token"
    token.write_text("secret\n")
    base = {
        "FLEET_BUS_URL": "nats://broker:4222",
        "FLEET_BUS_TOKEN_FILE": str(token),
    }
    with pytest.raises(CoordinatorNatsError, match="FLEET_BUS_USER"):
        load_nats_config({**base, "FLEET_BUS_USER": "console"})
    config = load_nats_config({**base, "FLEET_BUS_USER": COORDINATOR_NATS_USER})
    assert config.password == "secret"
    assert "secret" not in repr(config)


@pytest.mark.asyncio
async def test_connection_uses_coordinator_credentials(tmp_path):
    token = tmp_path / "token"
    token.write_text("secret")
    config = load_nats_config({
        "FLEET_BUS_URL": "nats://broker:4222",
        "FLEET_BUS_USER": "coordinator",
        "FLEET_BUS_TOKEN_FILE": str(token),
    })
    seen = {}

    async def fake_connect(**kwargs):
        seen.update(kwargs)
        return "connection"

    assert await connect_nats(config, connect=fake_connect) == "connection"
    assert seen == {
        "servers": ["nats://broker:4222"],
        "user": "coordinator",
        "password": "secret",
        "name": "yugo-coordinator",
        "inbox_prefix": b"_INBOX_coordinator",
        "max_reconnect_attempts": -1,
    }


@pytest.mark.asyncio
async def test_non_coordinator_publish_to_inbox_is_refused(tmp_path):
    """The negative control: removing the bot deny makes this test FAIL.

    A positive coordinator-only test would prove reachability, not ownership.
    The bot deliberately retains its broad ``fleet.>`` allow, matching the
    migration window, so the explicit inbox deny is the only thing stopping
    this publish.
    """
    binary = nats_server_binary()
    if binary is None:
        if __import__("os").environ.get("CI"):
            pytest.fail("CI must run the broker-backed coordinator authz test")
        pytest.skip("no nats-server binary")

    port = _free_port()
    source = (Path(__file__).resolve().parent.parent / "config/nats-coordinator-authz.conf").read_text()
    source = source.replace("CHANGEME_COORDINATOR", "coord-test")
    source = source.replace("CHANGEME_BOT", "bot-test")
    source = source.replace("CHANGEME_CONSOLE", "console-test")
    config_path = tmp_path / "nats.conf"
    source = source.replace(
        "# TEST_PROVISIONER",
        '{user: provisioner, password: "provision-test"}',
    )
    config_path.write_text(
        f'port: {port}\njetstream {{ store_dir: "{tmp_path / "js"}" }}\n{source}'
    )
    server = NatsServer(binary, config_path, port)
    server.start()
    coordinator = bot = console = provisioner = None
    try:
        provisioner = await nats.connect(server.url, user="provisioner", password="provision-test")
        js = provisioner.jetstream()
        await js.add_stream(name="FLEET_REQUEST", subjects=["fleet.*.request"])
        await js.add_stream(name="FLEET_INBOX", subjects=["fleet.*.inbox"])

        received = asyncio.Event()
        coordinator_violations = asyncio.Queue()

        async def on_coordinator_error(error):
            if "permissions violation" in str(error).lower():
                await coordinator_violations.put(error)

        coordinator = await nats.connect(
            server.url,
            user="coordinator",
            password="coord-test",
            error_cb=on_coordinator_error,
        )
        async def on_message(_msg):
            received.set()

        await coordinator.subscribe("fleet.vec.inbox", cb=on_message)
        await coordinator.flush()

        await coordinator.publish("fleet.vec.inbox", b"allowed")
        await coordinator.flush()
        await asyncio.wait_for(received.wait(), timeout=2)
        received.clear()

        # The pull-consumer role must not inherit JetStream administration.
        await coordinator.publish("$JS.API.STREAM.DELETE.FLEET_REQUEST", b"{}")
        await coordinator.flush()
        js_error = await asyncio.wait_for(coordinator_violations.get(), timeout=2)
        assert "publish" in str(js_error).lower()

        violations = asyncio.Queue()

        async def on_error(error):
            if "permissions violation" in str(error).lower():
                await violations.put(error)

        bot = await nats.connect(server.url, user="fleet-bot", password="bot-test", error_cb=on_error)

        own_request = asyncio.Event()

        async def on_own_request(_msg):
            own_request.set()

        await bot.subscribe("fleet.fleet-bot.request", cb=on_own_request)
        await bot.flush()
        await bot.publish("fleet.fleet-bot.request", b"migration path")
        await bot.flush()
        await asyncio.wait_for(own_request.wait(), timeout=2)

        # A JetStream PubAck is sent to the publisher-selected reply subject.
        # The bot's service import must keep that reply in BOT_FLEET_BOT rather
        # than reflecting it into FLEET's durable peer inbox.
        inbox_before = (await js.stream_info("FLEET_INBOX")).state.messages
        await bot.publish("fleet.vec.request", b"probe", reply="fleet.vec.inbox")
        await bot.flush()
        await asyncio.sleep(0.1)
        assert (await js.stream_info("FLEET_REQUEST")).state.messages == 2
        assert (await js.stream_info("FLEET_INBOX")).state.messages == inbox_before
        assert not received.is_set()

        foreign_received = asyncio.Event()

        async def on_foreign_message(_msg):
            foreign_received.set()

        # Both paths bypass coordinator interposition if a broad fleet.>
        # subscribe grant slips back into the bot role. Each refusal is a
        # negative control; deleting the per-bot restriction makes this test
        # time out waiting for the broker's violations.
        await bot.subscribe("fleet.vec.request", cb=on_foreign_message)
        await bot.subscribe("fleet.vec.inbox", cb=on_foreign_message)
        await bot.flush()
        subscribe_errors = [
            await asyncio.wait_for(violations.get(), timeout=2),
            await asyncio.wait_for(violations.get(), timeout=2),
        ]
        assert all("subscription" in str(error).lower() for error in subscribe_errors)
        await bot.publish("fleet.vec.request", b"must not reach peer")
        await coordinator.publish("fleet.vec.inbox", b"must not reach peer")
        await bot.flush()
        await coordinator.flush()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(foreign_received.wait(), timeout=0.15)
        received.clear()

        await bot.publish("fleet.vec.inbox", b"must not arrive")
        await bot.flush()
        error = await asyncio.wait_for(violations.get(), timeout=2)
        assert "publish" in str(error).lower()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(received.wait(), timeout=0.15)

        console_violations = asyncio.Queue()

        async def on_console_error(error):
            if "permissions violation" in str(error).lower():
                await console_violations.put(error)

        console = await nats.connect(
            server.url,
            user="console",
            password="console-test",
            error_cb=on_console_error,
        )
        await console.publish("fleet.vec.inbox", b"console must not publish")
        await console.flush()
        console_error = await asyncio.wait_for(console_violations.get(), timeout=2)
        assert "publish" in str(console_error).lower()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(received.wait(), timeout=0.15)

        # The denial is inbox-specific, not a accidentally-useless bot role.
        await bot.publish("fleet.vec.request", b"allowed")
        await bot.flush()
    finally:
        if bot is not None:
            await bot.close()
        if console is not None:
            await console.close()
        if coordinator is not None:
            await coordinator.close()
        if provisioner is not None:
            await provisioner.close()
        server.stop()
