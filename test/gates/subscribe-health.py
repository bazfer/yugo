#!/usr/bin/env python3
"""Issue #89: real-broker controls and a caused legacy-health regression.

Isolated copies only. Requires nats-server on PATH, Bun, pytest, and the pinned
Python adapter requirements. A green control is not a substitute for a mutant.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
BUN = shutil.which("bun") or str(ROOT / "node_modules/.bin/bun")
assert shutil.which("nats-server"), "install real nats-server before running this gate"


def check(label, command, cwd, env, red=False, contains=None):
    result = subprocess.run(command, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if (result.returncode != 0) != red:
        raise AssertionError(f"{label}: unexpected exit {result.returncode}\n{result.stdout}")
    if red and not ("fail" in result.stdout.lower()):
        raise AssertionError(f"{label}: not an assertion/test failure\n{result.stdout}")
    if contains is not None and contains not in result.stdout:
        raise AssertionError(f"{label}: failed for the wrong reason (missing {contains!r})\n{result.stdout}")
    print(f"{'RED detected' if red else 'GREEN control'}: {label}", flush=True)
    if red and contains is not None:
        print(next(line.strip() for line in result.stdout.splitlines() if contains in line), flush=True)


with tempfile.TemporaryDirectory(prefix=".subscribe-gate-", dir=ROOT) as directory:
    tmp = Path(directory)
    ignore = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")
    shutil.copytree(ROOT / "src", tmp / "src", ignore=ignore)
    shutil.copytree(ROOT / "yugo", tmp / "yugo", ignore=ignore)
    env = {**os.environ, "FLEET_BUS_ENABLED": "0", "PYTHONPATH": str(tmp / "yugo")}
    ts_cmd = [BUN, "test", str(tmp / "src/subscribe-health.test.ts")]
    py_cmd = [sys.executable, "-m", "pytest", "-q", str(tmp / "yugo/test/test_subscribe_health.py")]
    check("TS five real-broker cases plus transient-status control", ts_cmd, tmp, env)
    check("Python real-broker five-case suite", py_cmd, tmp, env)
    ts_file = tmp / "src/fleet-bus.ts"
    py_file = tmp / "yugo/fleet_bus.py"
    ts = ts_file.read_text()
    py = py_file.read_text()
    # Narrow mutant: leave refusal/state/audit reporting intact; remove ONLY
    # the physical heartbeat stop. Require the actual post-recovery beat assertion.
    start = ts.index('  private refuseSubject(')
    end = ts.index('  private verifySubscriptions()', start)
    cancellation = '    if (this.heartbeatTimer) clearInterval(this.heartbeatTimer)\n    this.heartbeatTimer = undefined\n'
    refusal = ts[start:end]
    assert refusal.count(cancellation) == 1
    ts_heartbeat = ts[:start] + refusal.replace(cancellation, '') + ts[end:]
    ts_guard = "if (!this.nc || this.nc.isClosed() || this.state !== 'connected') return"
    assert ts_heartbeat.count(ts_guard) == 1
    ts_heartbeat = ts_heartbeat.replace(ts_guard, 'if (!this.nc || this.nc.isClosed()) return')
    py_guard = 'if self._nc is None or self._nc.is_closed or self._state != "connected":'
    assert py.count(py_guard) == 1
    py_heartbeat = py.replace(py_guard, 'if self._nc is None or self._nc.is_closed:')
    ts_replay = ts_cmd + ["-t", "refused replay after reconnect"]
    py_replay = py_cmd + ["-k", "replayed_subscribe_refusal_after_reconnect"]
    ts_file.write_text(ts_heartbeat)
    py_file.write_text(py_heartbeat)
    check("TS heartbeat-only mutant retains initial unhealthy/refusal/audit reporting",
          ts_cmd + ["-t", "refused SUB is unhealthy"], tmp, env)
    check("TS heartbeat-only mutant emits beats after witnessed observer recovery",
          ts_replay, tmp, env, red=True,
          contains="error: heartbeat continued after observer recovery")
    check("Python heartbeat-only mutant emits beats after witnessed observer recovery",
          py_replay, tmp, env, red=True,
          contains="AssertionError: heartbeat continued after observer recovery")
    check("Python initial-refusal heartbeat-stop proof remains effective",
          py_cmd + ["-k", "refused_subject_is_unhealthy"], tmp, env, red=True,
          contains="AssertionError: a deaf bot must not publish liveness")
    ts_file.write_text(ts)
    py_file.write_text(py)

    # Positive controls for the new observation barrier: reconnect alone must
    # not allow silence to be measured when a fresh delivery is never received.
    ts_test = tmp / "src/subscribe-health.test.ts"
    py_test = tmp / "yugo/test/test_subscribe_health.py"
    ts_tests = ts_test.read_text()
    py_tests = py_test.read_text()
    assert ts_tests.count('pendingWitnesses.get(frame.id)?.(frame)') == 1
    assert py_tests.count('delivery.set_result(frame)') == 1
    ts_test.write_text(ts_tests.replace('pendingWitnesses.get(frame.id)?.(frame)', 'void frame'))
    py_test.write_text(py_tests.replace('delivery.set_result(frame)', 'pass  # discard witness delivery'))
    check("TS reconnect without fresh delivery is rejected, not mistaken for silence",
          ts_replay, tmp, env, red=True,
          contains="error: observer did not receive fresh status-subscription witness")
    check("Python reconnect without fresh delivery is rejected, not mistaken for silence",
          py_replay, tmp, env, red=True,
          contains="AssertionError: observer did not receive fresh status-subscription witness")
    ts_test.write_text(ts_tests)
    py_test.write_text(py_tests)

    ts_mutant = ts.replace('  private refuseSubject(subject: string): void {',
                           '  private refuseSubject(subject: string): void {\n    return')
    py_mutant = py.replace('    def _refuse_subject(self, subject: str) -> None:',
                           '    def _refuse_subject(self, subject: str) -> None:\n        return')
    ts_file.write_text(ts_mutant)
    py_file.write_text(py_mutant)
    ts_one = ts_cmd + ["-t", "refused SUB is unhealthy"]
    py_one = py_cmd + ["-k", "refused_subject_is_unhealthy"]
    check("TS removal of terminal refusal check breaks unhealthy/no-beat safety", ts_one, tmp, env, red=True)
    check("Python removal of terminal refusal check breaks unhealthy/no-beat safety", py_one, tmp, env, red=True)

    # Same refusal, but demand the old wrong outcome: connected + heartbeating.
    # This witness MUST pass with the checks removed and fail with them present.
    ts_test = tmp / "src/subscribe-health.test.ts"
    text = ts_test.read_text().replace('expect((f.bus.busStatus() as any).healthy).toBe(false)',
                                       'expect((f.bus.busStatus() as any).healthy).toBe(true)')
    text = text.replace("expect((f.bus.busStatus() as any).state).toBe('unhealthy')",
                        "expect((f.bus.busStatus() as any).state).toBe('connected')")
    text = text.replace('expect(f.beats).toHaveLength(0)', 'expect(f.beats.length).toBeGreaterThan(0)')
    text = text.replace('expect(drops).toHaveLength(1)', 'expect(drops).toHaveLength(0)')
    text = text.replace("    expect(drops[0]).toMatchObject({ subject: 'fleet.yugo.request', disposition: 'terminal' })", "")
    ts_test.write_text(text)
    py_test = tmp / "yugo/test/test_subscribe_health.py"
    text = py_test.read_text().replace('assert bus.bus_status()["healthy"] is False',
                                       'assert bus.bus_status()["healthy"] is True')
    text = text.replace('assert bus.bus_status()["state"] == "unhealthy"',
                        'assert bus.bus_status()["state"] == "connected"')
    text = text.replace('assert beats == [], "a deaf bot must not publish liveness"',
                        'assert beats, "legacy bug: a deaf bot advertises liveness"')
    text = text.replace('assert len(drops) == 1', 'assert len(drops) == 0')
    text = text.replace('        assert drops[0]["subject"] == "fleet.yugo.request"\n', '')
    text = text.replace('        assert drops[0]["disposition"] == "terminal"\n', '')
    py_test.write_text(text)
    check("TS check removed => legacy healthy-but-deaf witness goes GREEN", ts_one, tmp, env)
    check("Python check removed => legacy healthy-but-deaf witness goes GREEN", py_one, tmp, env)
    ts_file.write_text(ts)
    py_file.write_text(py)
    check("TS restored check rejects deliberately wrong healthy expectation", ts_one, tmp, env, red=True)
    check("Python restored check rejects deliberately wrong healthy expectation", py_one, tmp, env, red=True)
