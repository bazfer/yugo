"""Python conformance runner for the fleet-bus envelope v1 vectors.

Runs the normative vector set in vendored/envelope.v1.vectors.json against this
repo's ``fleet_bus.validate_envelope``. See the vector file's sibling README upstream
(artifice-ia/fleet-bus conformance/README.md) for the contract implemented here.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from fleet_bus import validate_envelope

IMPL = "python"
# This implementation's audit prefix on baton-field reasons. Stripped once, never
# substring-matched: a suffix match would let `invalid_hops` satisfy a vector
# expecting `invalid_hops_ceiling`, and those are different refusals.
REASON_PREFIX = "yugo_"

VECTORS_PATH = Path(__file__).resolve().parent.parent / "vendored" / "envelope.v1.vectors.json"
_SUITE = json.loads(VECTORS_PATH.read_text())
MAX_BYTES: int = _SUITE["max_bytes"]
ALLOWED: set[str] = set(_SUITE["allowed_from"])

_MISSING = object()


def _pad_to(target: int) -> dict:
    """Build an envelope whose compact UTF-8 encoding is exactly ``target`` bytes."""
    base = {
        "envelope_version": 1,
        "id": "size",
        "from": "deet",
        "kind": "text_message",
        "ts": "2026-09-02T00:00:00Z",
        "payload": {"pad": ""},
    }

    def encoded_length(value: dict) -> int:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    pad_length = target - encoded_length(base)
    if pad_length < 0:
        raise ValueError(f"target {target} is below the envelope floor")
    built = {**base, "payload": {"pad": "a" * pad_length}}
    actual = encoded_length(built)
    # Approximating here would silently turn the boundary vectors into ordinary
    # size vectors, which is the one thing they exist not to be.
    if actual != target:
        raise ValueError(f"generator missed: wanted {target} bytes, built {actual}")
    return built


GENERATORS = {
    "pad_to_exact_bytes": lambda: _pad_to(MAX_BYTES),
    "pad_to_one_over_bytes": lambda: _pad_to(MAX_BYTES + 1),
}


def _input_for(vector: dict):
    if "envelope_generator" in vector:
        generator = GENERATORS.get(vector["envelope_generator"])
        if generator is None:
            raise ValueError(f"unknown generator: {vector['envelope_generator']}")
        return generator()
    if "envelope" in vector:
        return vector["envelope"]
    if "envelope_raw" in vector:
        return vector["envelope_raw"]
    raise ValueError(f"vector {vector['name']} has no input")


def _unprefix(reason: str) -> str:
    return reason[len(REASON_PREFIX):] if reason.startswith(REASON_PREFIX) else reason


class ConformanceVectorTest(unittest.TestCase):
    def test_vectors(self) -> None:
        normative = divergent = 0
        for vector in _SUITE["vectors"]:
            with self.subTest(vector=vector["name"]):
                is_divergence = vector.get("status") == "known_divergence"
                # Divergences assert what this implementation does TODAY, so the
                # runner also fails when one is silently fixed — that forces the
                # vector file to be updated in the same change as the fix.
                expected = vector.get("actual", {}).get(IMPL, _MISSING) if is_divergence else vector["expect"]
                self.assertIsNot(
                    expected, _MISSING, f"divergence records no '{IMPL}' behaviour"
                )
                if is_divergence:
                    divergent += 1
                else:
                    normative += 1

                # Built directly, NOT through load_fleet_allowlist: an override
                # exists to hold a name the manifest normalizer refuses, and
                # running it through the code under test would hand the vector
                # back its own answer.
                allowed = set(vector["allowed_from_override"]) if "allowed_from_override" in vector else ALLOWED
                result = validate_envelope(_input_for(vector), allowed, max_bytes=MAX_BYTES)
                got = "accept" if result.ok else "reject"
                self.assertEqual(
                    got, expected, f"{vector['name']}: {result.error or ''}"
                )

                reason = vector.get("reason")
                # A divergence whose correct behaviour differs from today's is
                # asserted only on the accept/reject axis.
                if got == "reject" and reason and not (is_divergence and vector["expect"] != expected):
                    self.assertEqual(_unprefix(result.error), reason)

                if got == "accept" and "normalized_from" in vector:
                    self.assertEqual(result.envelope["from"], vector["normalized_from"])

        print(f"{IMPL}: {normative} normative + {divergent} divergence vectors")


if __name__ == "__main__":
    unittest.main()
