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
