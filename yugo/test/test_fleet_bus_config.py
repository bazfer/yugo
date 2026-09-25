"""
Import-time config-validation tests for the fleet-bus adapter.

Split of responsibility (SPEC §10 has no line for this, so v0.3a fixes the
split here and the tests are the spec):

    CONFIG fault  -> fatal at startup, naming the env var.
                     Only the operator can fix it, and a bot that boots
                     "fine" while permanently unable to authenticate or
                     validate a `from` claim is worse than one that refuses
                     to start. Same class as SPEC §10's persona-missing.

    TRANSIENT     -> not fatal. NATS unreachable is a network condition; the
                     bot serves Discord and retries forever.
                     Covered in test_fleet_bus_wiring.py.

Run in subprocesses, like test_bot_config.py, so module-scope validation can
raise cleanly without poisoning the parent's already-imported `bot`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import bot

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _bus_env(tmp_path: Path, **overrides) -> dict[str, str]:
    """A fully VALID bus env. Each test breaks exactly one key."""
    token = tmp_path / "token"
    token.write_text("s3cret\n", encoding="utf-8")
    manifest = tmp_path / "fleet-manifest.yaml"
    manifest.write_text("bot_names:\n  - yugo\n  - vec\n", encoding="utf-8")
    env = {
        "FLEET_BUS_ENABLED": "1",
        "BOT_NAME": "yugo",
        "FLEET_BUS_URL": "nats://127.0.0.1:1",
        "FLEET_BUS_USER": "",
        "FLEET_BUS_TOKEN_FILE": str(token),
        "FLEET_BUS_MANIFEST_PATH": str(manifest),
        "FLEET_BUS_AUDIT_LOG": str(tmp_path / "audit.jsonl"),
        # Load-bearing since this helper's callers now pin the dedup default.
        # `_import_bot` merges {**os.environ, **env_overrides}, so a developer
        # with YUGO_DEDUP_STORE_PATH exported gets a local-only failure on the
        # very test whose job is to pin that value — and it looks like a code
        # bug. Every other key this test depends on is set explicitly; so is
        # this one now.
        "YUGO_DEDUP_STORE_PATH": "",
    }
    env.update(overrides)
    return env


def _import_bot(env_overrides: dict[str, str], code: str = "import bot"):
    return subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, **env_overrides},
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )


def test_valid_bus_env_imports_cleanly(tmp_path):
    """Control. Without this, every abort test below could be passing for the
    wrong reason (e.g. a syntax error in fleet_bus.py aborts them all)."""
    r = _import_bot(
        _bus_env(tmp_path),
        "import bot; print(bot.BUS_CONFIG.bot_name, sorted(bot.BUS_CONFIG.allowed_from))",
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert r.stdout.strip() == "yugo ['vec', 'yugo']"


@pytest.mark.parametrize(
    "overrides,named_var",
    [
        ({"FLEET_BUS_TOKEN_FILE": ""}, "FLEET_BUS_TOKEN_FILE"),
        ({"FLEET_BUS_TOKEN_FILE": "/nonexistent/token"}, "FLEET_BUS_TOKEN_FILE"),
        ({"BOT_NAME": ""}, "BOT_NAME"),
        ({"BOT_NAME": "Not A Name"}, "BOT_NAME"),
        ({"FLEET_BUS_USER": "someone-else"}, "FLEET_BUS_USER"),
        ({"FLEET_BUS_MANIFEST_PATH": "/nonexistent/manifest.yaml"}, "FLEET_BUS_MANIFEST_PATH"),
        # `ohm` is a real fleet bot and a valid name, just not in THIS
        # manifest. The allowlist gates our own `from` claim too, so this bot
        # would boot clean and then drop its own heartbeat as
        # `from_claim_rejected` on every interval, forever.
        ({"BOT_NAME": "ohm"}, "BOT_NAME"),
    ],
)
def test_config_faults_abort_startup_naming_the_variable(tmp_path, overrides, named_var):
    r = _import_bot(_bus_env(tmp_path, **overrides))
    assert r.returncode != 0, (
        f"expected startup abort for {overrides}; stdout={r.stdout!r}"
    )
    assert "FleetBusConfigError" in r.stderr, r.stderr
    assert named_var in r.stderr, (
        f"abort message must NAME the offending variable so the operator is "
        f"not reading an anonymous traceback out of a restart-looping "
        f"container; got {r.stderr!r}"
    )


@pytest.mark.parametrize(
    "body",
    [
        "",  # empty file -> YAML None -> not a mapping
        "bot_names: []\n",
        "bot_names:\n",
        "- yugo\n",
        "bot_names:\n  - not a name\n",
        "bot_names: [yugo\n",  # unparseable YAML
    ],
    ids=["empty-file", "empty-list", "null-list", "not-a-mapping", "bad-entry", "bad-yaml"],
)
def test_unusable_manifest_aborts_startup(tmp_path, body):
    """Class coverage, not one instance: missing, empty, wrong-shaped,
    bad-entry and unparseable manifests must ALL abort.

    An empty allowlist is the dangerous one — it rejects every envelope
    including the bot's own heartbeat loopback, so the bot looks healthy on
    the wire and receives nothing forever.
    """
    manifest = tmp_path / "broken-manifest.yaml"
    manifest.write_text(body, encoding="utf-8")
    r = _import_bot(_bus_env(tmp_path, FLEET_BUS_MANIFEST_PATH=str(manifest)))
    assert r.returncode != 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "FLEET_BUS_MANIFEST_PATH" in r.stderr, r.stderr


def test_unset_and_unreadable_token_files_get_DIFFERENT_messages(tmp_path):
    """"Names the variable" is not enough on its own.

    Unset and unreadable are different operator fixes (add the env var vs fix
    the bind mount / permissions), and an implementation that let a blank
    `FLEET_BUS_TOKEN_FILE` fall through to `Path("").read_text()` would still
    abort naming the variable — with a message pointing at the wrong repair.
    """
    unset = _import_bot(_bus_env(tmp_path, FLEET_BUS_TOKEN_FILE=""))
    missing = _import_bot(_bus_env(tmp_path, FLEET_BUS_TOKEN_FILE="/nonexistent/token"))
    assert unset.returncode != 0 and missing.returncode != 0
    assert "FLEET_BUS_TOKEN_FILE is required" in unset.stderr, unset.stderr
    assert "FLEET_BUS_TOKEN_FILE is unreadable" in missing.stderr, missing.stderr


def test_empty_token_file_aborts_startup(tmp_path):
    """A zero-byte token is a mount that did not land. Connecting with an
    empty password would just loop on Authorization Violation forever."""
    token = tmp_path / "empty-token"
    token.write_text("", encoding="utf-8")
    r = _import_bot(_bus_env(tmp_path, FLEET_BUS_TOKEN_FILE=str(token)))
    assert r.returncode != 0
    assert "FLEET_BUS_TOKEN_FILE" in r.stderr


def test_unreadable_token_file_aborts_startup(tmp_path):
    """Permission-denied, not just missing — a root-owned secret on a non-root
    bot is the realistic deployment failure."""
    token = tmp_path / "chmod-000-token"
    token.write_text("s3cret", encoding="utf-8")
    os.chmod(token, 0o000)
    if os.access(token, os.R_OK):  # running as root: the chmod means nothing
        pytest.skip("running as root; chmod 000 is not enforced")
    try:
        r = _import_bot(_bus_env(tmp_path, FLEET_BUS_TOKEN_FILE=str(token)))
    finally:
        os.chmod(token, 0o600)
    assert r.returncode != 0
    assert "FLEET_BUS_TOKEN_FILE" in r.stderr


def test_bus_disabled_ignores_every_broken_bus_var(tmp_path):
    """`FLEET_BUS_ENABLED=0` must be a hard off switch, not a soft one.

    All the vars are pointed at garbage. `.env.example` ships them blank, CI's
    smoke gate feeds that file to the container, and a startup abort here
    would restart-loop every yugo deployment that never opted into the bus.
    """
    r = _import_bot(
        _bus_env(
            tmp_path,
            FLEET_BUS_ENABLED="0",
            BOT_NAME="",
            FLEET_BUS_TOKEN_FILE="/nonexistent/token",
            FLEET_BUS_MANIFEST_PATH="/nonexistent/manifest.yaml",
        ),
        "import bot; print(bot.BUS_CONFIG)",
    )
    assert r.returncode == 0, f"stdout={r.stdout!r} stderr={r.stderr!r}"
    assert r.stdout.strip() == "None"


def test_env_example_defaults_keep_the_bus_off():
    """`.env.example` IS the operator's `.env` template and CI's smoke-gate
    env-file. If it ever ships FLEET_BUS_ENABLED=1 (or drops the key), every
    documented quick-start would abort on the missing token file."""
    text = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "\nFLEET_BUS_ENABLED=0\n" in text


def test_defaults_match_the_documented_env_surface(tmp_path):
    """Defaults are a contract with `.env.example` and SPEC §5: blank optional
    keys must resolve to the documented values, not crash and not silently
    pick something else."""
    r = _import_bot(
        _bus_env(tmp_path, FLEET_BUS_URL="", FLEET_BUS_AUDIT_LOG=""),
        "import bot; c = bot.BUS_CONFIG; "
        "print(c.url, c.audit_log_path, c.user, c.heartbeat_interval_s, "
        "c.max_envelope_bytes, c.dedup_store_path, c.plugin_version)",
    )
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert r.stdout.split() == [
        "nats://nats:4222",
        "/root/.claude/fleet-bus-log.jsonl",
        "yugo",  # FLEET_BUS_USER blank -> BOT_NAME
        "30.0",  # SPEC/TS heartbeat cadence
        "1044480",  # DEFAULT_MAX_ENVELOPE_BYTES, matches fleet-bus.ts
        # SPEC §6.4 asserts the Python adapter is a FILE-BACKED participant in a
        # shared store. Nothing enforced that. Drop the `or "/var/lib/yugo/..."`
        # in load_config_from_env and this path becomes "" -> falsy -> the
        # `:memory:` constructor fallback, silently making §6.4 wrong again
        # while the whole suite stayed green. §6.4 previously carried exactly
        # that wrong claim, so pin the default rather than narrate it.
        "/var/lib/yugo/yugo-dedup.sqlite",
        # Read from the module rather than pinned as a literal: this test is
        # about DEFAULTS, and a hardcoded version turns every slice bump into
        # an unrelated red. `test_packaging` is what pins the version itself.
        bot.YUGO_VERSION,
    ]


def test_password_is_not_in_the_config_repr(tmp_path):
    """FleetBusConfig ends up in tracebacks and `print(BUS_CONFIG)` debugging.
    The NATS credential must not ride along into a log or a Discord paste."""
    r = _import_bot(
        _bus_env(tmp_path), "import bot; print(repr(bot.BUS_CONFIG))"
    )
    assert r.returncode == 0, f"stderr={r.stderr!r}"
    assert "s3cret" not in r.stdout, r.stdout
    assert "FleetBusConfig(" in r.stdout
