"""
Import-time config-validation tests for bot.py.

Run in subprocesses so bot's module-scope validation can raise cleanly
without polluting the parent's already-imported `bot` module.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_import_bot(env_overrides: dict[str, str]) -> subprocess.CompletedProcess:
    """Import `bot` in a subprocess with the given env, return the result."""
    env = {**os.environ, **env_overrides}
    return subprocess.run(
        [sys.executable, "-c", "import bot"],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )


def test_history_max_turns_zero_fails_loud_at_import():
    """`HISTORY_MAX_TURNS=0` MUST raise ValueError at import — not silently
    mute mid-message inside a `store[]` bound-check that produces empty
    Discord replies + noisy tracebacks."""
    r = _run_import_bot({"HISTORY_MAX_TURNS": "0"})
    assert r.returncode != 0, f"expected import failure; stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "HISTORY_MAX_TURNS must be a positive integer" in r.stderr, (
        f"expected validation error in stderr; got {r.stderr!r}"
    )


def test_history_max_turns_negative_fails_loud_at_import():
    r = _run_import_bot({"HISTORY_MAX_TURNS": "-1"})
    assert r.returncode != 0
    assert "HISTORY_MAX_TURNS must be a positive integer" in r.stderr


def test_history_max_turns_valid_positive_imports_cleanly():
    """Sanity: the happy path still works — a positive integer imports OK."""
    r = _run_import_bot({"HISTORY_MAX_TURNS": "5"})
    assert r.returncode == 0, f"import failed: stdout={r.stdout!r} stderr={r.stderr!r}"


def test_bus_history_max_turns_zero_fails_loud_at_import():
    """v0.3b's bus-lane depth gets the same treatment as the Discord one.

    A second bounded knob is exactly where a per-call-site check rots — hence
    `_env_int(..., positive=True)` — but "the helper handles it" is a claim,
    and this is the test that makes it one the CI can check.
    """
    r = _run_import_bot({"BUS_HISTORY_MAX_TURNS": "0"})
    assert r.returncode != 0, f"expected import failure; stderr={r.stderr!r}"
    assert "BUS_HISTORY_MAX_TURNS must be a positive integer" in r.stderr, (
        f"expected validation error naming the var; got {r.stderr!r}"
    )


def test_bus_history_max_turns_blank_defaults_to_ten():
    """`.env.example` ships it with a value, but the operator's `.env` is a
    hand-edited copy: a blanked-out optional key must fall through to the
    documented default, not crash the container at import."""
    r = subprocess.run(
        [sys.executable, "-c", "import bot; print(repr(bot.BUS_HISTORY_MAX_TURNS))"],
        env={**os.environ, "BUS_HISTORY_MAX_TURNS": ""},
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    assert r.returncode == 0, (
        f"import failed on blank BUS_HISTORY_MAX_TURNS: stderr={r.stderr!r}"
    )
    assert r.stdout.strip() == "10", f"expected default 10; got {r.stdout!r}"


# ---------- blank-value holes in optional env parsers ----------
#
# `.env.example` ships several optional keys with a bare `KEY=` (blank value).
# Any raw `int(os.environ.get(...))` on such a key hits `int("")` and crashes
# the container at import — the documented quick-start (`cp .env.example .env
# && docker compose up`) would restart-loop before serving one message. These
# tests pin the fix (`_env_int` normalizes blank → default) so the class of
# regression is caught here AND in CI's `--env-file .env.example` smoke gate.


def test_history_max_turns_blank_defaults_to_ten():
    """A blank `HISTORY_MAX_TURNS=` in the env MUST NOT crash `int()` — it
    falls through to the documented default (10), matching the optional-env
    convention `.env.example` uses for every unset optional slot.

    Bite check: revert the `_env_int` call to
    `int(os.environ.get("HISTORY_MAX_TURNS", "10"))` — the subprocess exits
    nonzero with `invalid literal for int() with base 10: ''` and this test
    fails on returncode.
    """
    r = subprocess.run(
        [
            sys.executable,
            "-c",
            "import bot; print(repr(bot.HISTORY_MAX_TURNS))",
        ],
        env={**os.environ, "HISTORY_MAX_TURNS": ""},
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    assert r.returncode == 0, (
        f"import failed on blank HISTORY_MAX_TURNS: "
        f"stdout={r.stdout!r} stderr={r.stderr!r}"
    )
    assert r.stdout.strip() == "10", (
        f"expected default 10; got stdout={r.stdout!r}"
    )


def test_guild_id_blank_becomes_none():
    """`.env.example` ships `GUILD_ID=` blank as the "no guild filter" slot.
    Blank MUST resolve to None at import, NOT crash on `int('')`.

    Bite check: revert to `int(os.environ.get("GUILD_ID", "0")) or None` —
    the subprocess exits nonzero on the raw `int('')` and this test fails
    on returncode. (The trailing `or None` never runs because `int` raises
    first.)
    """
    r = subprocess.run(
        [
            sys.executable,
            "-c",
            "import bot; print(repr(bot.GUILD_ID))",
        ],
        env={**os.environ, "GUILD_ID": ""},
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    assert r.returncode == 0, (
        f"import failed on blank GUILD_ID: "
        f"stdout={r.stdout!r} stderr={r.stderr!r}"
    )
    assert r.stdout.strip() == "None", (
        f"expected None; got stdout={r.stdout!r}"
    )


# ---------- persona-missing must be fatal at import (SPEC §4.5, §10) ----------


def test_persona_missing_both_paths_fails_loud_at_import(tmp_path):
    """SPEC §4.5 ("Persona-missing at startup = fatal (bot refuses to run)")
    and §10 ("Persona file missing — fatal at startup") both require this.

    Test setup: copy bot.py + history.py to a scratch dir with NO IDENTITY.md,
    override PERSONA_FILE to a nonexistent path, `cwd=scratch` so `import bot`
    resolves to the scratch copy — the fallback `Path(__file__).parent /
    "IDENTITY.md"` then points at `scratch/IDENTITY.md` (missing) instead of
    the real repo IDENTITY.md. Both persona sources unreadable → load_persona
    MUST raise RuntimeError at module import, not silently return a generic
    assistant string.

    Bite check: revert load_persona to `return "You are a helpful assistant."`
    on both-missing — the subprocess exits 0 (bot imports fine with the
    fallback string) and this test fails on returncode.
    """
    scratch = tmp_path / "yugo_scratch"
    scratch.mkdir()
    # Copy EVERY top-level module, not a hand-listed pair: when v0.3a added
    # fleet_bus.py this test started failing on ModuleNotFoundError instead of
    # the persona error it exists to assert — right returncode, wrong reason.
    # Globbing keeps the next module addition from re-opening that hole.
    for module in _REPO_ROOT.glob("*.py"):
        shutil.copy(module, scratch / module.name)
    # Deliberately do NOT copy IDENTITY.md — that's the fallback the SPEC
    # requires as a valid persona source.
    env = {**os.environ, "PERSONA_FILE": str(tmp_path / "does-not-exist.md")}
    r = subprocess.run(
        [sys.executable, "-c", "import bot"],
        env=env,
        capture_output=True,
        text=True,
        cwd=str(scratch),
    )
    assert r.returncode != 0, (
        f"expected import failure; SPEC §4.5/§10 require persona-missing "
        f"to be fatal at startup; stdout={r.stdout!r} stderr={r.stderr!r}"
    )
    assert "Persona load failed" in r.stderr, (
        f"expected named 'Persona load failed' error; got stderr={r.stderr!r}"
    )


# --- SANDBOX_MODE (v0.4e) -------------------------------------------------
#
# The flag's only job is to keep the toggle honest, so these tests are about
# what the bot REFUSES to do. A stub that parsed the value and continued would
# pass any test asserting merely that the bot starts.


def test_sandbox_mode_unset_defaults_to_reduced():
    r = _run_import_bot({"SANDBOX_MODE": ""})
    assert r.returncode == 0, f"blank SANDBOX_MODE must start; stderr={r.stderr!r}"


def test_sandbox_mode_reduced_starts():
    r = _run_import_bot({"SANDBOX_MODE": "reduced"})
    assert r.returncode == 0, f"stderr={r.stderr!r}"


def test_sandbox_mode_full_aborts_rather_than_downgrading_silently():
    """`full` MUST NOT resolve to reduced. The operator sets it when the threat
    model changed; running reduced under a full label is a lie nothing later
    contradicts, and a log warning would scroll while the belief persisted."""
    r = _run_import_bot({"SANDBOX_MODE": "full"})
    assert r.returncode != 0, f"expected abort; stdout={r.stdout!r} stderr={r.stderr!r}"
    assert "SANDBOX_MODE=full is not implemented" in r.stderr, r.stderr


def test_sandbox_mode_unknown_value_aborts():
    """`strict` is an operator who means `full`. Resolving it to `reduced` is
    the same failure as accepting `full`, by a different route."""
    r = _run_import_bot({"SANDBOX_MODE": "strict"})
    assert r.returncode != 0, f"expected abort; stderr={r.stderr!r}"
    assert "SANDBOX_MODE must be one of" in r.stderr, r.stderr


def test_sandbox_mode_is_case_and_whitespace_insensitive():
    """`Full` is the same operator intent as `full` — it must hit the same
    refusal, not fall through to the unknown-value branch or to reduced."""
    r = _run_import_bot({"SANDBOX_MODE": "  FULL  "})
    assert r.returncode != 0
    assert "SANDBOX_MODE=full is not implemented" in r.stderr, r.stderr
    r = _run_import_bot({"SANDBOX_MODE": "Reduced"})
    assert r.returncode == 0, f"stderr={r.stderr!r}"
