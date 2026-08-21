"""Shared pytest configuration for local-code-agent's test suite.

agent/api_server.py imports bot.models.schemas directly from the sibling
telegram_agent_bot repository - the same PYTHONPATH requirement the
launch-agent-api.* scripts already handle for real usage. This does the
equivalent for the test suite: look for telegram_agent_bot as a sibling
directory (the documented, expected layout) and add it to sys.path if
found, so `pytest` just works out of the box for anyone with both repos
checked out side by side - matching how the project is actually shipped.

If it's genuinely not there, tests/test_api_server.py skips itself cleanly
(see its own pytest.importorskip call) rather than failing the whole
collection with a confusing import error.
"""
from __future__ import annotations

import sys
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parent.parent
_CANDIDATE_BOT_PATH = _AGENT_DIR.parent / "telegram_agent_bot"

if (_CANDIDATE_BOT_PATH / "bot" / "models" / "schemas.py").exists():
    sys.path.insert(0, str(_CANDIDATE_BOT_PATH))
