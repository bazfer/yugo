"""Cross-port test subprocess; never used in a consumer."""
import json
import sys
import time

import dedup_store as d
from dedup_verification import read_boot_id

data = json.load(sys.stdin)
if data["mode"] == "actual":
    before = time.monotonic_ns()
    store = d.DurableEnvelopeDedupStore(":memory:")
    store._db.execute("INSERT INTO envelope_dedup_v2 VALUES ('e',100,'original','pending','owner',160,?,?)",
                      (data["boot"], data["deadline"]))
    result = store.claim("e", "rival", 100)
    after = time.monotonic_ns()
    print(json.dumps(dict(before=str(before), after=str(after), duplicate=result[0], boot=read_boot_id())))
else:
    mono = [100000]
    d.time.monotonic_ns = lambda: mono[0] * 1000000
    store = d.DurableEnvelopeDedupStore(":memory:")
    output = []
    for wall, value in data["steps"]:
        mono[0] = value
        result = store.claim("e", "original" if not output else "rival", wall / 1000)
        row = store._db.execute("SELECT req_id,state,lease_boot_id,lease_until_mono_ms FROM envelope_dedup_v2").fetchone()
        output.append([result[0], result[1], *row[1:]])
    print(json.dumps(output))
