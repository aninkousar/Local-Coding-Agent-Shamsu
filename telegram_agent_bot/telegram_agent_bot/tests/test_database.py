"""Unit tests for database/db.py - upsert semantics and log pagination in
particular, since a foreign-key/upsert bug here caused a real crash during
development (logging against a project that hadn't been cached locally
yet - see bot/handlers/prompt.py's fix)."""
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bot.models.schemas import LogLevel, Project, ProjectStatus
from database.db import Database


@pytest.fixture
async def db(tmp_path: Path):
    instance = await Database.connect(tmp_path / "test.db")
    yield instance
    await instance.close()


def _project(**overrides):
    defaults = dict(
        id="PRJ-001", name="Test Project", status=ProjectStatus.RUNNING, progress=50,
        created_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    return Project(**defaults)


async def test_upsert_and_get_project(db):
    await db.upsert_project(_project())
    fetched = await db.get_project("PRJ-001")
    assert fetched is not None
    assert fetched.name == "Test Project"
    assert fetched.progress == 50


async def test_upsert_updates_in_place_not_duplicate(db):
    await db.upsert_project(_project(progress=10))
    await db.upsert_project(_project(progress=90, status=ProjectStatus.COMPLETED))
    all_projects = await db.list_projects()
    assert len(all_projects) == 1
    assert all_projects[0].progress == 90


async def test_current_task_is_persisted(db):
    """Regression test for a real bug: current_task existed in the Pydantic
    model but was never actually stored in the schema."""
    await db.upsert_project(_project(current_task="Implementing JWT middleware"))
    fetched = await db.get_project("PRJ-001")
    assert fetched.current_task == "Implementing JWT middleware"


async def test_get_nonexistent_project_returns_none(db):
    assert await db.get_project("PRJ-DOES-NOT-EXIST") is None


async def test_logging_against_a_cached_project_works(db):
    """The exact bug fixed in bot/handlers/prompt.py: adding a log entry
    requires the project to already exist locally, due to the foreign key
    constraint - this confirms that constraint is real and enforced."""
    await db.upsert_project(_project())
    await db.add_log("PRJ-001", LogLevel.INFO, "Test log message")
    logs = await db.get_logs("PRJ-001")
    assert len(logs) == 1
    assert logs[0].message == "Test log message"


async def test_logging_against_an_uncached_project_raises(db):
    """Confirms the foreign key constraint is genuinely enforced - logging
    against a project that was never upserted must fail, not silently
    succeed with an orphaned row."""
    with pytest.raises(Exception):
        await db.add_log("PRJ-NEVER-CACHED", LogLevel.INFO, "This should fail")


async def test_log_pagination(db):
    await db.upsert_project(_project())
    for i in range(5):
        await db.add_log("PRJ-001", LogLevel.INFO, f"Log {i}")

    page1 = await db.get_logs("PRJ-001", limit=3, offset=0)
    page2 = await db.get_logs("PRJ-001", limit=3, offset=3)
    assert len(page1) == 3
    assert len(page2) == 2
    # most-recent-first ordering
    assert page1[0].message == "Log 4"

    total = await db.count_logs("PRJ-001")
    assert total == 5
