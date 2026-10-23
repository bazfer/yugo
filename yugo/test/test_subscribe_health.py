"""Real broker refusals: SUB returns a handle, but that is not acceptance."""
import asyncio
import json
import os
import uuid

import nats
import pytest

from fleet_bus import FleetBus, FleetBusConfig
from test.nats_server import NatsServer, _free_port, nats_server_binary

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("provision_bus_stores")]


@pytest.fixture
def restricted_broker(tmp_path):
    servers = []
    def start(refused):
        binary = nats_server_binary()
        if not binary:
            if os.environ.get("CI"):
                pytest.fail("subscribe-health tests require a real nats-server")
            pytest.skip("no nats-server binary")
        config = tmp_path / f"restricted-{len(servers)}.conf"
        port = _free_port()
        config.write_text(f'''port: {port}
authorization {{ users: [
 {{user: yugo, password: test, permissions: {{publish: ["fleet.>"], subscribe: {{allow: ["fleet.yugo.>", "fleet.broadcast.>"], deny: {json.dumps(refused)}}}}}}},
 {{user: vec, password: test}}
] }}
''')
        server = NatsServer(binary, config, port)
        server.start()
        servers.append(server)
        return server
    yield start
    for server in servers:
        server.stop()


class Observer:
    def __init__(self):
        self.reconnected = asyncio.Event()
        self.pending = {}

    async def on_reconnected(self):
        self.reconnected.set()

    async def fresh_witness(self, peer):
        identifier = f"observer-{uuid.uuid4()}"
        delivery = asyncio.get_running_loop().create_future()
        self.pending[identifier] = delivery
        try:
            # Same subscription as beats; never mistake the witness for a beat.
            await peer.publish("fleet.yugo.status", json.dumps({"envelope_version": 1,
                "id": identifier, "from": "vec", "to": "yugo", "kind": "observer_witness",
                "ts": "2026-10-23T15:00:00Z", "payload": {"nonce": identifier}}).encode())
            await peer._flush_pending(force_flush=True)
            await peer.flush()
            try:
                frame = await asyncio.wait_for(delivery, 5)
            except TimeoutError as error:
                raise AssertionError("observer did not receive fresh status-subscription witness") from error
            assert frame["id"] == identifier
            assert frame["payload"]["nonce"] == identifier
        finally:
            self.pending.pop(identifier, None)

    async def recover(self, peer):
        await asyncio.wait_for(self.reconnected.wait(), 5)
        await peer._flush_pending(force_flush=True)
        await peer.flush()
        await self.fresh_witness(peer)


async def setup_bus(broker, tmp_path, observer=None):
    if observer is None:
        observer = Observer()
    records = []
    class Audit:
        def record(self, direction, subject, **fields):
            records.append(dict(dir=direction, subject=subject, **fields))
    bus = FleetBus(FleetBusConfig(bot_name="yugo", url=broker.url, user="yugo", password="test",
                    allowed_from=frozenset({"yugo", "vec"}), dedup_store_path=str(tmp_path / "dedup.sqlite"),
                    plugin_version="test", heartbeat_interval_s=0.03, reconnect_time_wait_s=0.03), Audit())
    peer = await nats.connect(broker.url, user="vec", password="test", reconnected_cb=observer.on_reconnected)
    beats = []
    async def on_beat(msg):
        frame = json.loads(msg.data)
        if frame.get("kind") == "observer_witness":
            delivery = observer.pending.get(frame["id"])
            if delivery is not None and not delivery.done():
                delivery.set_result(frame)
        elif frame.get("kind") == "status_heartbeat":
            await collect(beats, msg)
    await peer.subscribe("fleet.yugo.status", cb=on_beat)
    await peer.flush()
    await bus._connect()
    return bus, peer, beats, records


async def collect(target, msg):
    target.append(json.loads(msg.data))


async def test_refused_subject_is_unhealthy_and_never_heartbeats(restricted_broker, tmp_path):
    bus, peer, beats, records = await setup_bus(restricted_broker(["fleet.yugo.request"]), tmp_path)
    try:
        assert bus.connection.is_connected, "positive control: broker kept connection alive"
        loop = asyncio.create_task(bus._heartbeat_loop())
        await asyncio.sleep(0.13)
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        assert bus.bus_status()["healthy"] is False
        assert bus.bus_status()["state"] == "unhealthy"
        assert beats == [], "a deaf bot must not publish liveness"
        drops = [r for r in records if r.get("reason") == "yugo_subscribe_refused"]
        assert len(drops) == 1
        assert drops[0]["subject"] == "fleet.yugo.request"
        assert drops[0]["disposition"] == "terminal"
    finally:
        await bus._teardown()
        await peer.close()


async def test_refused_one_named_and_granted_lane_still_receives(restricted_broker, tmp_path):
    bus, peer, _, records = await setup_bus(restricted_broker(["fleet.yugo.result"]), tmp_path)
    try:
        assert bus.bus_status()["subjects_refused"] == ["fleet.yugo.result"]
        await peer.publish("fleet.yugo.request", json.dumps({"envelope_version": 1, "id": "survivor",
            "kind": "text_message", "from": "vec", "to": "yugo", "ts": "2026-10-23T15:00:00Z", "payload": {}}).encode())
        await peer.flush()
        for _ in range(100):
            if any(r.get("id") == "survivor" and r["dir"] == "in" for r in records):
                break
            await asyncio.sleep(0.01)
        assert any(r.get("id") == "survivor" and r["dir"] == "in" for r in records)
        snapshot = bus.bus_status()
        snapshot["subjects_refused"].clear()
        assert bus.bus_status()["subjects_refused"] == ["fleet.yugo.result"]
    finally:
        await bus._teardown()
        await peer.close()


async def test_all_granted_is_healthy_and_heartbeats(restricted_broker, tmp_path):
    bus, peer, beats, _ = await setup_bus(restricted_broker([]), tmp_path)
    loop = asyncio.create_task(bus._heartbeat_loop())
    try:
        assert bus.bus_status()["healthy"] is True
        assert bus.bus_status()["state"] == "connected"
        assert bus.bus_status()["subjects_refused"] == []
        for _ in range(100):
            if len(beats) >= 2: break
            await asyncio.sleep(0.01)
        assert len(beats) >= 2
        assert beats[0]["payload"]["healthy"] is True
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        await bus._teardown()
        await peer.close()


async def test_positive_control_broker_really_refuses_but_retains_local_handle(restricted_broker):
    errors = []
    async def error_cb(error): errors.append(str(error))
    broker = restricted_broker(["fleet.yugo.result"])
    nc = await nats.connect(broker.url, user="yugo", password="test", error_cb=error_cb)
    try:
        async def noop(_): pass
        sub = await nc.subscribe("fleet.yugo.result", cb=noop)
        await nc._flush_pending(force_flush=True)
        await nc.flush()
        assert nc.is_connected
        assert sub.subject == "fleet.yugo.result"
        assert sub in nc._subs.values(), "nats-py retains even a REFUSED subscription"
        assert any('subscription to "fleet.yugo.result"' in e.lower() for e in errors), errors
    finally:
        await nc.close()


async def test_replayed_subscribe_refusal_after_reconnect_is_terminal(restricted_broker, tmp_path):
    broker = restricted_broker([])
    observer = Observer()
    bus, peer, beats, records = await setup_bus(broker, tmp_path, observer)
    loop = asyncio.create_task(bus._heartbeat_loop())
    try:
        await asyncio.sleep(0.06)
        assert beats, "positive control: healthy beats before authz changes"
        broker.stop()
        config = broker._config_path
        config.write_text(config.read_text().replace('deny: []', 'deny: ["fleet.yugo.request"]'))
        broker.start()
        for _ in range(200):
            if bus.bus_status()["state"] == "unhealthy": break
            await asyncio.sleep(0.01)
        assert bus.bus_status()["subjects_refused"] == ["fleet.yugo.request"]
        assert bus.bus_status()["healthy"] is False
        assert len([r for r in records if r.get("reason") == "yugo_subscribe_refused"]) == 1
        await observer.recover(peer)  # Reconnect AND a received fresh nonce on the beat subscription.
        count = len(beats)
        await asyncio.sleep(0.13)  # Multiple heartbeat periods, now actually observed.
        await observer.fresh_witness(peer)  # Delivery also bookends the observation window.
        assert len(beats) == count, (
            f"heartbeat continued after observer recovery: before={count} after={len(beats)}"
        )
        assert len([r for r in records if r.get("reason") == "yugo_subscribe_refused"]) == 1
    finally:
        loop.cancel()
        await asyncio.gather(loop, return_exceptions=True)
        await bus._teardown()
        await peer.close()
