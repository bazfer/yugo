"""Green/red/green against an expendable R1 checkout; always restore sources."""
import os
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parent.parent
checkout = Path(os.environ["RELEASE1_CHECKOUT"]).resolve()
expected = "table envelope_dedup_v2 has 7 columns but 6 values were supplied"

for port, relative, unit, command in [
    ("Python", "yugo/fleet_bus.py", "s",
     [sys.executable, str(root / "conformance/release1-wide-insert.py")]),
    ("TypeScript", "src/fleet-bus.ts", "ms",
     [os.environ.get("BUN", "bun"), str(root / "conformance/release1-wide-insert.ts")]),
]:
    source = checkout / relative
    original = source.read_bytes()
    named = (f"INSERT OR IGNORE INTO envelope_dedup_v2 "
             f"(envelope_id,first_seen_{unit},req_id,state,lease_owner,lease_until_{unit}) VALUES").encode()
    assert original.count(named) == 1, f"{port}: expected one production INSERT"
    env = {**os.environ, "PYTHONPATH": str(checkout / "yugo")}

    def run():
        return subprocess.run(command, env=env, cwd=root, capture_output=True, text=True)

    before = run()
    assert before.returncode == 0, before.stdout + before.stderr
    try:
        source.write_bytes(original.replace(named, b"INSERT OR IGNORE INTO envelope_dedup_v2 VALUES"))
        mutated = run()
        assert mutated.returncode != 0, f"{port}: positional mutation survived"
        assert expected in mutated.stdout + mutated.stderr, mutated.stdout + mutated.stderr
    finally:
        source.write_bytes(original)
    after = run()
    assert after.returncode == 0, after.stdout + after.stderr
    print(f"PASS: {port} named INSERT green/red/green; {expected}")
