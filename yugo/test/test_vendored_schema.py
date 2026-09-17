"""
v0.3e — vendored fleet-bus envelope schema guards.

Everything the Dockerfile hash-check catches at build time gets a pytest twin
that runs against the source tree, so the two verification surfaces stay in
sync. A build-time hash-check on its own is enough to fail a fresh `docker
build`, but a warm builder that already has the vendored/ layer cached will
happily reuse a mismatched pair; the pytest twin catches that class in CI
before the Dockerfile ever executes.

Not covered here: matching the vendored copy against upstream at the pinned
version. That's a network-dependent check kept build-only — pytest stays hermetic.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")

_ROOT = Path(__file__).resolve().parent.parent
_VENDORED = _ROOT / "vendored"
_SCHEMA_PATH = _VENDORED / "envelope.v1.schema.json"
_HASH_PATH = _VENDORED / "FLEET_BUS_SCHEMA_SHA256"
_VERSION_PATH = _VENDORED / "FLEET_BUS_VERSION"


def test_vendored_dir_exists() -> None:
    assert _VENDORED.is_dir(), "vendored/ dir is missing; 3e ships schema pinning under vendored/"


def test_vendored_schema_hash_matches_recorded() -> None:
    """The three files must be co-consistent: hashing the schema on disk must
    produce exactly the value in FLEET_BUS_SCHEMA_SHA256. If someone edits one
    without the other, the build step catches it — this test catches it earlier
    on a warm builder that would reuse the vendored/ layer."""
    schema_bytes = _SCHEMA_PATH.read_bytes()
    recorded = _HASH_PATH.read_text().strip()
    actual = hashlib.sha256(schema_bytes).hexdigest()
    assert actual == recorded, (
        f"FLEET_BUS_SCHEMA_SHA256 records {recorded!r} but hashing "
        f"envelope.v1.schema.json produces {actual!r}. Regenerate with "
        f"`sha256sum vendored/envelope.v1.schema.json | awk '{{print $1}}' "
        f"> vendored/FLEET_BUS_SCHEMA_SHA256` after any deliberate schema bump."
    )


def test_vendored_schema_is_valid_json() -> None:
    """A well-formed JSON-Schema-shaped object, so the Dockerfile's `cmp` step
    can't silently accept a byte-identical pair of garbage files."""
    data = json.loads(_SCHEMA_PATH.read_text())
    assert isinstance(data, dict), "vendored schema is not a JSON object"
    assert data.get("$schema", "").startswith("https://json-schema.org/"), (
        "vendored schema is missing / has wrong $schema draft URL"
    )
    assert data.get("title") == "fleet-bus envelope v1", (
        "vendored schema title changed — did we vendor the right file?"
    )


def test_vendored_version_is_full_commit_sha() -> None:
    """FLEET_BUS_VERSION must be exactly 40 lowercase-hex chars: a full commit
    SHA, no mutable refs. Ohm's PR #11 review caught that a length-only guard
    accepts `mainxxx` — a branch-shaped value that defeats the reproducibility
    property this slice claims. Both this test AND the Dockerfile RUN step
    enforce the full-SHA shape (independent surfaces per
    `mutation-test-the-controls`)."""
    raw = _VERSION_PATH.read_bytes()
    assert raw, "FLEET_BUS_VERSION file is empty; 3e pins to an upstream commit"
    text = raw.decode("utf-8")
    # Command substitution `$(cat ...)` strips trailing newlines, so a
    # trailing LF isn't a Dockerfile failure — but the SHA validation regex
    # uses fullmatch and would reject `<sha>\n`, so this stays canonical.
    assert _FULL_SHA_RE.fullmatch(text), (
        f"FLEET_BUS_VERSION must be exactly 40 lowercase-hex chars "
        f"(a full commit SHA), got {text!r}. Mutable refs (branch names, "
        f"short SHAs, movable tags) defeat the pin's reproducibility guarantee."
    )


# Mutation matrix — every value that a naïve length-only guard would accept
# but the full-SHA guard rejects. Ohm's review: `mainxxx` passes a length>=7
# check. Add the whole shape space so a future weakening of the regex fails
# loudly instead of silently reopening the hole.
@pytest.mark.parametrize(
    "bad_value, why",
    [
        ("mainxxx", "branch-shaped (7 chars, non-hex)"),
        ("main", "branch name (<40 chars)"),
        ("820e4d8", "short SHA (7 hex chars but not 40)"),
        ("820E4D856AA1D27ACAC5E308C010CFC4161BDA39", "uppercase hex"),
        ("820e4d856aa1d27acac5e308c010cfc4161bda3", "39 hex chars (one short of full SHA)"),
        ("820e4d856aa1d27acac5e308c010cfc4161bda399", "41 hex chars (one over)"),
        ("v1.0.0", "release-tag-shaped"),
        ("HEAD", "symbolic ref"),
        ("", "empty string"),
        ("820e4d856aa1d27acac5e308c010cfc4161bda39\n", "trailing newline"),
        ("g820e4d856aa1d27acac5e308c010cfc4161bda3", "non-hex char (g) at position 0"),
    ],
)
def test_full_sha_regex_rejects_mutations(bad_value: str, why: str) -> None:
    """Any value that would defeat the pin's reproducibility guarantee gets
    rejected by the same regex both pytest and the Dockerfile use."""
    assert not _FULL_SHA_RE.fullmatch(bad_value), (
        f"regex accepted a mutation it shouldn't have ({why}): {bad_value!r}"
    )


def test_full_sha_regex_accepts_real_pinned_value() -> None:
    """Positive control — the actual value shipped MUST pass. If someone
    tightens the regex further and forgets to update the pin, this test
    surfaces the mismatch immediately."""
    text = _VERSION_PATH.read_bytes().decode("utf-8")
    assert _FULL_SHA_RE.fullmatch(text), (
        f"the shipped FLEET_BUS_VERSION {text!r} does not match "
        f"the full-SHA regex — either the pin regressed or the regex "
        f"was tightened past the shipped value"
    )
