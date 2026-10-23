#!/usr/bin/env python3
"""Mutation gate for #12. Each mutant MUST go red; controls MUST go green.

Uses isolated copies, never edits the checkout. Requires node_modules (Bun,
TypeScript) and Python's pytest/pytest-asyncio plus the adapter dependencies.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
BUN = shutil.which("bun") or str(ROOT / "node_modules/.bin/bun")
TSC = ROOT / "node_modules/.bin/tsc"


def check(label, command, cwd, *, red=False, contains=None, env=None):
    result = subprocess.run(command, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    ok = result.returncode != 0 if red else result.returncode == 0
    if contains is not None:
        ok = ok and contains in result.stdout
    if not ok:
        raise AssertionError(f"{label}: unexpected exit {result.returncode}\n{result.stdout}")
    print(f"{'RED detected' if red else 'GREEN control'}: {label}", flush=True)


with tempfile.TemporaryDirectory(prefix=".drop-gate-", dir=ROOT) as directory:
    tmp = Path(directory)
    shutil.copytree(ROOT / "src", tmp / "src")
    ts_map = (ROOT / "src/drop-disposition.ts").read_text()
    py_map = (ROOT / "yugo/drop_disposition.py").read_text()
    py_bus = (ROOT / "yugo/fleet_bus.py").read_text()
    ts_bus = (ROOT / "src/fleet-bus.ts").read_text()
    shutil.copy(ROOT / "yugo/test/test_drop_disposition.py", tmp / "test_drop_disposition.py")
    (tmp / "drop_disposition.py").write_text(py_map)
    (tmp / "fleet_bus.py").write_text(py_bus)
    env = {**os.environ, "PYTHONPATH": f"{tmp}:{ROOT / 'yugo'}"}
    ts_tests = [BUN, "test", str(tmp / "src/drop-disposition.test.ts")]
    py_tests = [sys.executable, "-m", "pytest", "-q", "--confcutdir", str(tmp), str(tmp / "test_drop_disposition.py")]
    compile_ts = [str(TSC), "--noEmit", "--strict", "--skipLibCheck", "--target", "ES2022", "--module", "ESNext", "--moduleResolution", "bundler", str(tmp / "src/drop-disposition.ts")]
    import_py = [sys.executable, "-c", "import drop_disposition"]
    check("TS unmodified map compiles", compile_ts, tmp)
    check("Python unmodified map imports", import_py, tmp, env=env)
    check("TS notice and manifest assertions", ts_tests, tmp)
    check("Python notice assertions", py_tests, tmp, env=env)

    for label, ts_mutant, py_mutant in [
        ("delete classification",
         ts_map.replace("  malformed_json: 'terminal',\n", ""),
         py_map.replace('    DropReason.MALFORMED_JSON: Disposition.TERMINAL,\n', "")),
        ("add unclassified reason",
         ts_map.replace("export type DropReason =", "export type DropReason =\n  | 'new_unclassified_reason'"),
         py_map.replace("class DropReason(str, Enum):", 'class DropReason(str, Enum):\n    NEW_UNCLASSIFIED_REASON = "new_unclassified_reason"')),
    ]:
        (tmp / "src/drop-disposition.ts").write_text(ts_mutant)
        (tmp / "drop_disposition.py").write_text(py_mutant)
        check(f"TS {label}", compile_ts, tmp, red=True, contains="error TS")
        check(f"Python {label}", import_py, tmp, red=True, contains="AssertionError: unclassified drop reason", env=env)
    (tmp / "src/drop-disposition.ts").write_text(ts_map)
    (tmp / "drop_disposition.py").write_text(py_map)

    for label, ts_mutant, py_mutant in [
        ("remove notice loop guard",
         ts_bus.replace("envelope.kind === 'drop_notice' || ", ""),
         py_bus.replace('        if context.get("kind") == "drop_notice":\n            return\n', "")),
        ("notify duplicates",
         ts_bus.replace("disposition === 'duplicate' || ", ""),
         py_bus.replace('disposition == "duplicate" or ', "")),
        ("silence transient drops",
         ts_bus.replace('this.nc.publish(`fleet.${sender}.request`, new TextEncoder().encode(JSON.stringify(notice)))', 'void notice'),
         py_bus.replace('await self._nc.publish(f"fleet.{sender}.request",\n                                   _canonical_json(notice).encode("utf-8"))', 'pass')),
    ]:
        assert ts_mutant != ts_bus and py_mutant != py_bus, label
        (tmp / "src/fleet-bus.ts").write_text(ts_mutant)
        (tmp / "fleet_bus.py").write_text(py_mutant)
        check(f"TS {label}", ts_tests, tmp, red=True, contains="fail")
        check(f"Python {label}", py_tests, tmp, red=True, contains="failed", env=env)
    (tmp / "src/fleet-bus.ts").write_text(ts_bus)
    (tmp / "fleet_bus.py").write_text(py_bus)

    # A successful suppression must never become a paid failure-notice turn.
    suppression_mutant = py_bus.replace(
        'self._audit.record("in", subject, event="suppression", reason=reason.value, **fields)',
        'self._record_drop(subject, reason=DropReason.INJECTION_FAILED, '
        'context={"id": fields.get("id"), "from": "deet", "kind": "text_message"})')
    assert suppression_mutant != py_bus
    (tmp / "fleet_bus.py").write_text(suppression_mutant)
    check("successful suppression emits failure notice", py_tests, tmp,
          red=True, contains="failed", env=env)
    (tmp / "fleet_bus.py").write_text(py_bus)

    # Informational events must remain outside classification AND still emit.
    (tmp / "src/drop-disposition.ts").write_text(ts_map.replace("export const DROP_DISPOSITION: Record<DropReason, Disposition> = {", "export const DROP_DISPOSITION: Record<DropReason, Disposition> = {\n  fleet_manifest_loaded: 'terminal',"))
    check("manifest misclassified as drop", ts_tests, tmp, red=True, contains="fail")
    (tmp / "src/drop-disposition.ts").write_text(ts_map)
    for reason in ["fleet_manifest_loaded", "fleet_manifest_load_failed", "fleet_manifest_reload_failed"]:
        mutant = ts_bus.replace(f"this.audit({{ reason: '{reason}'", f"void ({{ reason: '{reason}'")
        assert mutant != ts_bus, reason
        (tmp / "src/fleet-bus.ts").write_text(mutant)
        check(f"silence {reason}", ts_tests, tmp, red=True, contains="fail")
    (tmp / "src/fleet-bus.ts").write_text(ts_bus)

    # Positive controls for the assertion harness, not merely process failures.
    ts_test = (tmp / "src/drop-disposition.test.ts").read_text()
    (tmp / "src/drop-disposition.test.ts").write_text(ts_test.replace("envelope_id: 'original'", "envelope_id: 'WRONG'"))
    py_test = (tmp / "test_drop_disposition.py").read_text()
    (tmp / "test_drop_disposition.py").write_text(py_test.replace('"envelope_id": "original"', '"envelope_id": "WRONG"'))
    check("TS wrong expected payload (positive control)", ts_tests, tmp, red=True, contains="Expected")
    check("Python wrong expected payload (positive control)", py_tests, tmp, red=True, contains="AssertionError", env=env)
