"""Tests for agent/permissions.py and agent/auto_permissions.py.

PermissionManager (interactive) and AutoApprovePermissionManager (bridge mode)
duck-type an identical interface by design - ToolRegistry works with either
one interchangeably. That's exactly what the parametrized fixture below is
for: the structural safety guarantees (allowed_roots, hard_denylist) must
hold IDENTICALLY across both, so those tests run once, against both
implementations, rather than being duplicated per-class and risking one
copy drifting out of sync with the other.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from agent.auto_permissions import AutoApprovePermissionManager
from agent.permissions import PermissionManager


def _make_interactive(allowed_roots, hard_denylist):
    return PermissionManager(allowed_roots=allowed_roots, hard_denylist=hard_denylist)


def _make_auto_approve(allowed_roots, hard_denylist):
    return AutoApprovePermissionManager(allowed_roots=allowed_roots, hard_denylist=hard_denylist)


@pytest.fixture(params=[_make_interactive, _make_auto_approve],
                ids=["PermissionManager", "AutoApprovePermissionManager"])
def perm(request, tmp_path):
    """Parametrized fixture - every test using this fixture runs TWICE, once
    per permission manager implementation, against the identical allowed_roots
    setup. For PermissionManager, Prompt.ask is patched to always return "y"
    so these shared tests exercise the SAFETY checks (which run before any
    prompt), not the interactive approval flow itself - that's tested
    separately below, only against PermissionManager."""
    root = tmp_path / "project"
    root.mkdir()
    manager = request.param([root.resolve()], hard_denylist=["rm -rf /", "DROP DATABASE"])
    with patch("agent.permissions.Prompt.ask", return_value="y"):
        yield manager, root


# --- Shared structural safety: both implementations must behave identically ------

def test_read_outside_allowed_roots_blocked(perm):
    manager, root = perm
    outside = root.parent / "outside.txt"
    outside.write_text("secret")
    assert manager.request_read(outside) is False


def test_read_inside_allowed_roots_permitted(perm):
    manager, root = perm
    inside = root / "file.txt"
    inside.write_text("hello")
    assert manager.request_read(inside) is True


def test_write_outside_allowed_roots_blocked(perm):
    manager, root = perm
    outside = root.parent / "outside.txt"
    assert manager.request_write(outside) is False


def test_write_inside_allowed_roots_permitted(perm):
    manager, root = perm
    inside = root / "new_file.txt"
    assert manager.request_write(inside) is True


def test_read_batch_blocked_if_any_path_outside_root(perm):
    manager, root = perm
    inside = root / "a.txt"
    inside.write_text("x")
    outside = root.parent / "b.txt"
    # even though the FIRST path is fine, the batch must be blocked entirely
    # if ANY path in it is outside allowed_roots
    assert manager.request_read_batch([inside, outside]) is False


def test_write_batch_blocked_if_any_path_outside_root(perm):
    manager, root = perm
    inside = root / "a.txt"
    outside = root.parent / "b.txt"
    assert manager.request_write_batch([inside, outside]) is False


def test_command_matching_hard_denylist_blocked(perm):
    manager, _ = perm
    assert manager.request_command("sudo rm -rf / --no-preserve-root") is False


def test_command_not_matching_denylist_permitted(perm):
    manager, _ = perm
    assert manager.request_command("ls -la") is True


def test_command_matching_denylist_blocked_regardless_of_surrounding_text(perm):
    """Substring match, not exact match - a denylisted pattern anywhere in
    the command string should block it, not just an exact match."""
    manager, _ = perm
    assert manager.request_command("echo hi && DROP DATABASE production") is False


def test_path_resolution_defeats_traversal(perm):
    """A path outside allowed_roots crafted via '..' traversal must still be
    blocked - _within_allowed_roots resolves the path first, so this isn't
    just a string-prefix check that traversal could defeat."""
    manager, root = perm
    traversal = root / ".." / ".." / "etc" / "passwd"
    assert manager.request_read(traversal) is False


# --- PermissionManager-specific: the actual interactive approval flow -------------

@pytest.fixture
def interactive_perm(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    return PermissionManager(allowed_roots=[root.resolve()], hard_denylist=["rm -rf /"]), root


def test_interactive_deny_by_default(interactive_perm):
    manager, root = interactive_perm
    target = root / "file.txt"
    with patch("agent.permissions.Prompt.ask", return_value="n"):
        assert manager.request_read(target) is False


def test_interactive_always_grants_this_specific_path_only(interactive_perm):
    manager, root = interactive_perm
    target_a = root / "a.txt"
    target_b = root / "b.txt"
    with patch("agent.permissions.Prompt.ask", return_value="always"):
        assert manager.request_read(target_a) is True

    # A DIFFERENT path must still prompt again - "always" was scoped to
    # target_a specifically, not a blanket grant.
    with patch("agent.permissions.Prompt.ask", return_value="n") as mock_ask_2:
        assert manager.request_read(target_b) is False
        mock_ask_2.assert_called_once()

    # But re-requesting the SAME path must NOT prompt again.
    with patch("agent.permissions.Prompt.ask") as mock_ask_3:
        assert manager.request_read(target_a) is True
        mock_ask_3.assert_not_called()


def test_interactive_session_grants_all_future_reads(interactive_perm):
    manager, root = interactive_perm
    target_a = root / "a.txt"
    target_b = root / "b.txt"
    with patch("agent.permissions.Prompt.ask", return_value="session"):
        assert manager.request_read(target_a) is True

    # "session" for reads must now cover ANY file, without prompting again.
    with patch("agent.permissions.Prompt.ask") as mock_ask:
        assert manager.request_read(target_b) is True
        mock_ask.assert_not_called()


def test_interactive_session_read_grant_does_not_cover_writes(interactive_perm):
    """A session-wide read grant must not silently also grant writes - these
    are tracked as separate trust flags specifically so a broad 'yes' to
    reading doesn't quietly escalate into permission to modify files."""
    manager, root = interactive_perm
    target = root / "a.txt"
    with patch("agent.permissions.Prompt.ask", return_value="session"):
        assert manager.request_read(target) is True

    with patch("agent.permissions.Prompt.ask", return_value="n") as mock_ask:
        assert manager.request_write(target) is False
        mock_ask.assert_called_once()


def test_interactive_write_always_scoped_by_write_prefix(interactive_perm):
    """A write 'always' grant is stored as 'write:{path}', distinct from a
    read 'always' grant on the same path - confirms the two don't
    accidentally collide in the same trust set."""
    manager, root = interactive_perm
    target = root / "a.txt"
    with patch("agent.permissions.Prompt.ask", return_value="always"):
        assert manager.request_write(target) is True

    # A read on the SAME path must still prompt - write trust doesn't imply read trust.
    with patch("agent.permissions.Prompt.ask", return_value="n") as mock_ask:
        assert manager.request_read(target) is False
        mock_ask.assert_called_once()


def test_interactive_command_always_scoped_to_exact_command_string(interactive_perm):
    manager, _ = interactive_perm
    with patch("agent.permissions.Prompt.ask", return_value="always"):
        assert manager.request_command("npm install") is True

    # A DIFFERENT command string must still prompt.
    with patch("agent.permissions.Prompt.ask", return_value="n") as mock_ask:
        assert manager.request_command("npm run build") is False
        mock_ask.assert_called_once()


def test_interactive_db_write_session_grant(interactive_perm):
    manager, _ = interactive_perm
    with patch("agent.permissions.Prompt.ask", return_value="session"):
        assert manager.request_db_write("Insert a row") is True
    with patch("agent.permissions.Prompt.ask") as mock_ask:
        assert manager.request_db_write("Insert another row") is True
        mock_ask.assert_not_called()


# --- AutoApprovePermissionManager-specific: on_action logging visibility ----------

def test_auto_approve_logs_every_approved_action(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    logged = []
    manager = AutoApprovePermissionManager(
        allowed_roots=[root.resolve()], hard_denylist=["rm -rf /"],
        on_action=logged.append,
    )
    target = root / "file.txt"
    assert manager.request_read(target) is True
    assert len(logged) == 1
    assert "Read" in logged[0]


def test_auto_approve_logs_blocked_actions_too(tmp_path):
    """'No interactive prompt' must not also mean 'no visibility' -
    blocked/denied actions must be logged just as much as approved ones."""
    root = tmp_path / "project"
    root.mkdir()
    logged = []
    manager = AutoApprovePermissionManager(
        allowed_roots=[root.resolve()], hard_denylist=["rm -rf /"],
        on_action=logged.append,
    )
    outside = root.parent / "outside.txt"
    assert manager.request_read(outside) is False
    assert len(logged) == 1
    assert "Blocked" in logged[0]


def test_auto_approve_works_with_no_on_action_callback(tmp_path):
    """on_action is optional - must not raise if omitted."""
    root = tmp_path / "project"
    root.mkdir()
    manager = AutoApprovePermissionManager(allowed_roots=[root.resolve()], hard_denylist=[])
    assert manager.request_read(root / "file.txt") is True


def test_auto_approve_command_denylist_logs_block_reason(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    logged = []
    manager = AutoApprovePermissionManager(
        allowed_roots=[root.resolve()], hard_denylist=["rm -rf /"],
        on_action=logged.append,
    )
    assert manager.request_command("rm -rf / --no-preserve-root") is False
    assert any("denylist" in msg.lower() for msg in logged)


def test_auto_approve_request_action_and_db_write_always_true_when_permitted():
    """request_action and request_db_write have no allowed_roots concept -
    auto-approve means genuinely always true for these, per the class's own
    documented contract."""
    manager = AutoApprovePermissionManager(allowed_roots=[], hard_denylist=[])
    assert manager.request_action("Open browser preview") is True
    assert manager.request_db_write("Insert a row") is True
