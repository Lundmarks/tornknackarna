"""
Shared test setup. Runs before any project module is imported, so modules
that resolve data/ paths at import time use a throwaway folder instead of the
real data/ directory.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="tornknackarna-tests-")
os.environ.setdefault("ESPARVEN_TEAM_ID", "67")
for name in ("DISCORD_BOT_TOKEN", "DISCORD_READY_CHANNEL_ID", "DISCORD_GUILD_ID", "CURRENT_SEASON_ID"):
    os.environ.pop(name, None)

import pytest  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_html():
    return lambda name: (FIXTURES / name).read_text(encoding="utf-8")
