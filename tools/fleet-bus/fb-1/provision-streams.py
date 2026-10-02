"""Provision the fleet-bus JetStream streams (FB-1).

Idempotent: add_stream on an existing name falls through to update_stream, so
re-running after a config change converges rather than erroring.
"""
import asyncio, os, sys
import nats
from nats.js.api import StreamConfig

URL = os.environ["FLEET_BUS_URL"]
BOT = os.environ.get("BOT_NAME", "deet")

STREAMS = [
    ("FLEET_REQUEST", ["fleet.*.request"]),
    ("FLEET_RESULT", ["fleet.*.result"]),
]
# 7d retention, 100k per subject, discard oldest. max_age is SECONDS here —
# nats-py converts to nanoseconds itself, and passing ns silently overflows
# into a Go time.Duration unmarshal error rather than a large window.
COMMON = dict(max_age=7 * 24 * 3600, max_msgs_per_subject=100_000,
              discard="old", storage="file", retention="limits")

async def main():
    nc = await nats.connect(URL, inbox_prefix=f"_INBOX_{BOT}".encode())
    js = nc.jetstream()
    for name, subjects in STREAMS:
        cfg = StreamConfig(name=name, subjects=subjects, **COMMON)
        try:
            info = await js.add_stream(cfg)
            verb = "created"
        except Exception:
            info = await js.update_stream(cfg)
            verb = "updated"
        c = info.config
        print(f"{verb} {name}: subjects={c.subjects} max_age={c.max_age} "
              f"per_subject={c.max_msgs_per_subject} discard={c.discard} storage={c.storage}")
    await nc.close()

asyncio.run(main())
