"""Test fixtures.

bot.py reads several env vars at import time (v0.1 shape). Set safe stubs so
tests that import `bot` don't blow up in `os.environ[...]`. Individual tests
still monkeypatch behaviour they care about (e.g. `bot.HISTORY_MAX_TURNS`).
"""

import os
import sys
from pathlib import Path

# Make the repo root importable so `import history` / `import bot` works when
# pytest is invoked from any directory.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Pre-populate the env vars bot.py demands at import time. These are fake
# values; tests never touch Discord or a real LLM provider.
os.environ.setdefault("DISCORD_BOT_TOKEN", "test-token")
os.environ.setdefault("CHANNEL_ID", "12345")
os.environ.setdefault("MODEL", "openai/gpt-4o-mini")
os.environ.setdefault("HISTORY_MAX_TURNS", "3")
# Point PERSONA_FILE at the bundled IDENTITY.md so bot.load_persona() succeeds
# on any dev machine (default /root/persona.md is not readable outside the
# container).
os.environ.setdefault("PERSONA_FILE", str(_ROOT / "IDENTITY.md"))


def pytest_configure(config):
    """Register the marks this suite uses, so an unknown-mark warning stays a
    real signal rather than routine noise."""
    config.addinivalue_line(
        "markers",
        "filesystem_policy: test is ABOUT the coordinator's filesystem policy, so it "
        "opts out of the fixture that pins detection to a local filesystem.",
    )

# Opt-in only: broker integration tests retain real file-backed durable stores.
# Startup-refusal tests deliberately do NOT request this fixture.
import pytest


@pytest.fixture
def provision_bus_stores(monkeypatch):
    import fleet_bus
    from dedup_admin import provision

    base = fleet_bus.DurableEnvelopeDedupStore

    class ProvisionedStore(base):
        def __init__(self, path, *args, **kwargs):
            if path != ":memory:":
                record = str(path) + ".verification.json"
                provision(str(path), record, "python", "integration-test",
                          dict(device_path="test", mount_point="unresolved",
                               fstype="unresolved", mount_id_source="unresolved",
                               backing="local-virtual", determined_by="test fixture",
                               inspected_at="2026-09-24T19:00:00Z"),
                          [dict(process="test", user="test", path=str(path), method="fixture")],
                          record_only=Path(path).exists())
                monkeypatch.setenv("YUGO_DEDUP_VERIFICATION_RECORD", record)
            super().__init__(path, *args, **kwargs)

    monkeypatch.setattr(fleet_bus, "DurableEnvelopeDedupStore", ProvisionedStore)
