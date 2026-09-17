"""Versioned coordinator sqlite state and JetStream durable reconciliation."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import sqlite3

SCHEMA_VERSION = 1
INSTANCE_DDL = """CREATE TABLE IF NOT EXISTS coordinator_instance (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  pid INTEGER NOT NULL,
  acquired_at TEXT NOT NULL
)"""
MARKER_DDL = """CREATE TABLE IF NOT EXISTS coordinator_relay_marker (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  state TEXT NOT NULL CHECK (state IN ('intent', 'created')),
  stream_name TEXT NOT NULL,
  durable_name TEXT NOT NULL,
  consumer_created TEXT,
  stream_created TEXT,
  CHECK (state = 'intent' OR
         (consumer_created IS NOT NULL AND stream_created IS NOT NULL))
)"""
RESET_COMMAND = "coordinator_main.py accept-durable-reset"


class CoordinatorStateError(RuntimeError):
    def __init__(self, reason: str, detail: str):
        self.reason = reason
        super().__init__(f"{reason}: {detail}")


@dataclass(frozen=True)
class DatabasePreflight:
    version: int
    legacy_6b1: bool


def preflight(connection: sqlite3.Connection) -> DatabasePreflight:
    """Inspect before the lock's first schema write."""
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise CoordinatorStateError(
            "coordinator_state_version_newer",
            f"state schema version {version} is newer than supported version {SCHEMA_VERSION}",
        )
    tables = {
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if version == 0 and tables and "coordinator_instance" not in tables:
        raise CoordinatorStateError(
            "coordinator_state_foreign", "version-0 sqlite file is not a coordinator state database"
        )
    return DatabasePreflight(version, version == 0 and "coordinator_instance" in tables)


def migrate(connection: sqlite3.Connection, preflight_result: DatabasePreflight) -> None:
    """Migrate under the already-held exclusive lock."""
    if preflight_result.version == SCHEMA_VERSION:
        return
    connection.execute("BEGIN")
    try:
        connection.execute(INSTANCE_DDL)
        connection.execute(MARKER_DDL)
        # 6b-1 created its durable but had no marker. Intent routes that upgrade
        # through the recoverable intent/present reconciliation row.
        if preflight_result.legacy_6b1:
            connection.execute(
                "INSERT OR IGNORE INTO coordinator_relay_marker "
                "(id,state,stream_name,durable_name) VALUES (1,'intent',?,?)",
                ("FLEET_REQUEST", "yugo-coordinator-request"),
            )
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def marker(connection: sqlite3.Connection):
    return connection.execute(
        "SELECT state,stream_name,durable_name,consumer_created,stream_created "
        "FROM coordinator_relay_marker WHERE id=1"
    ).fetchone()


def write_intent(connection: sqlite3.Connection, stream: str, durable: str) -> None:
    connection.execute(
        "INSERT INTO coordinator_relay_marker (id,state,stream_name,durable_name) "
        "VALUES (1,'intent',?,?)",
        (stream, durable),
    )


def finalize(connection: sqlite3.Connection, stream: str, durable: str, consumer_created, stream_created) -> None:
    connection.execute(
        "UPDATE coordinator_relay_marker SET state='created', stream_name=?, durable_name=?, "
        "consumer_created=?, stream_created=? WHERE id=1",
        (stream, durable, consumer_created.isoformat(), stream_created.isoformat()),
    )


def _as_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def assert_live_shape(consumer_info, stream_info, *, stream: str, durable: str) -> None:
    from nats.js.api import DeliverPolicy

    config = consumer_info.config
    if config.deliver_policy != DeliverPolicy.NEW or config.filter_subject != "fleet.*.request":
        raise CoordinatorStateError(
            "coordinator_durable_policy_drift",
            f"durable {durable} must use deliver_policy NEW and filter fleet.*.request; run {RESET_COMMAND}",
        )
    if stream_info.config.name != stream:
        raise CoordinatorStateError("coordinator_durable_policy_drift", f"unexpected stream; run {RESET_COMMAND}")


async def _live(js, stream: str, durable: str):
    from nats.js.errors import NotFoundError
    try:
        consumer = await js.consumer_info(stream, durable)
    except NotFoundError:
        consumer = None
    stream_info = await js.stream_info(stream)
    return consumer, stream_info


async def reconcile(connection: sqlite3.Connection, relay) -> None:
    """Reconcile the sqlite intent/created marker with the external durable."""
    stream, durable = "FLEET_REQUEST", "yugo-coordinator-request"
    row = marker(connection)
    consumer, stream_info = await _live(relay._js, stream, durable)

    if row is None and consumer is not None:
        raise CoordinatorStateError(
            "coordinator_durable_foreign", f"unowned durable {durable} exists; run {RESET_COMMAND}"
        )
    if row is not None and row[0] == "created" and consumer is None:
        raise CoordinatorStateError(
            "coordinator_durable_deleted", f"durable {durable} was deleted; run {RESET_COMMAND}"
        )
    if row is None:
        write_intent(connection, stream, durable)
        row = marker(connection)

    await relay.open()  # create when absent, otherwise bind

    # The pre-bind lookup is only useful for deciding how to reconcile the
    # marker. Consumer identity and configuration can change while open() is
    # awaiting the broker, so all safety assertions use a fresh lookup.
    consumer, stream_info = await _live(relay._js, stream, durable)
    if consumer is None:
        raise CoordinatorStateError(
            "coordinator_durable_deleted", f"durable {durable} disappeared during bind; run {RESET_COMMAND}"
        )

    assert_live_shape(consumer, stream_info, stream=stream, durable=durable)
    if row[0] == "created":
        if row[1] != stream or row[2] != durable:
            raise CoordinatorStateError("coordinator_durable_regenerated", f"marker identity changed; run {RESET_COMMAND}")
        stored_consumer = _as_datetime(row[3])
        stored_stream = _as_datetime(row[4])
        if abs((consumer.created - stored_consumer).total_seconds()) > 1 or stream_info.created != stored_stream:
            raise CoordinatorStateError(
                "coordinator_durable_regenerated", f"durable generation changed; run {RESET_COMMAND}"
            )
    else:
        finalize(connection, stream, durable, consumer.created, stream_info.created)


async def accept_reset(
    connection: sqlite3.Connection, js, *, acknowledge_traffic_gap: bool = False
) -> bool:
    """Accept the live generation, or clear a stale marker for a missing durable.

    Returns True when clearing the marker acknowledges a traffic gap, and
    False when an existing durable was re-stamped.
    """
    stream, durable = "FLEET_REQUEST", "yugo-coordinator-request"
    consumer, stream_info = await _live(js, stream, durable)
    if consumer is None:
        if not acknowledge_traffic_gap:
            raise CoordinatorStateError(
                "coordinator_durable_deleted",
                "clearing the marker abandons traffic published while the durable was absent; "
                f"run {RESET_COMMAND} --acknowledge-traffic-gap",
            )
        connection.execute("DELETE FROM coordinator_relay_marker WHERE id=1")
        return True
    assert_live_shape(consumer, stream_info, stream=stream, durable=durable)
    connection.execute(
        "INSERT INTO coordinator_relay_marker "
        "(id,state,stream_name,durable_name,consumer_created,stream_created) VALUES (1,'created',?,?,?,?) "
        "ON CONFLICT(id) DO UPDATE SET state=excluded.state, stream_name=excluded.stream_name, "
        "durable_name=excluded.durable_name, consumer_created=excluded.consumer_created, "
        "stream_created=excluded.stream_created",
        (stream, durable, consumer.created.isoformat(), stream_info.created.isoformat()),
    )
    return False
