"""FB-1 post-apply verification against the LIVE bus.

Uses a throwaway subject inside the stream's wildcard so it cannot collide
with a real bot's traffic: `fleet.fb1verify.request` matches `fleet.*.request`
but no bot subscribes to it.
"""
import asyncio, os, sys
import nats
from nats.js.api import ConsumerConfig, AckPolicy, DeliverPolicy

URL = os.environ["FLEET_BUS_URL"]
SUBJ = "fleet.fb1verify.request"

async def main(phase):
    nc = await nats.connect(URL, inbox_prefix=b"_INBOX_deet")
    js = nc.jetstream()
    if phase == "publish":
        await js.add_consumer("FLEET_REQUEST", ConsumerConfig(
            durable_name="fb1_verify", deliver_policy=DeliverPolicy.ALL,
            ack_policy=AckPolicy.EXPLICIT, filter_subject=SUBJ))
        for i in range(3):
            ack = await js.publish(SUBJ, f'{{"n":{i}}}'.encode())
            print(f"  published seq={ack.seq}")
    elif phase == "consume":
        sub = await js.pull_subscribe(SUBJ, durable="fb1_verify")
        msgs = await sub.fetch(10, timeout=5)
        print(f"  survived restart: {[m.data.decode() for m in msgs]}")
        for m in msgs:
            await m.ack()
    elif phase == "cleanup":
        await js.delete_consumer("FLEET_REQUEST", "fb1_verify")
        info = await js.stream_info("FLEET_REQUEST")
        print(f"  consumer removed; FLEET_REQUEST holds {info.state.messages} msg(s)")
    await nc.close()

asyncio.run(main(sys.argv[1]))
