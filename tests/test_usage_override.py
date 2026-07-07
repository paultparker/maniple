"""Tests for src/maniple_mcp/usage_override.py -- the shared read/advance/
clear/install logic behind the override_usage_pause & clear_usage_override
MCP tools and the `maniple usage-override` / `install-global-usage-guard`
CLI subcommands.
"""

from __future__ import annotations

import json
import time

import pytest

from maniple_mcp import usage_override


@pytest.fixture()
def override_dir(tmp_path):
    d = tmp_path / "usage_override"
    d.mkdir()
    return d


class TestReadOverride:
    def test_missing_file_returns_none(self, override_dir):
        assert usage_override.read_override("worker1", override_dir) is None

    def test_expired_returns_none(self, override_dir):
        (override_dir / "worker1.json").write_text(
            json.dumps({"threshold": 0.90, "expires_at": time.time() - 10})
        )
        assert usage_override.read_override("worker1", override_dir) is None

    def test_malformed_json_returns_none(self, override_dir):
        (override_dir / "worker1.json").write_text("not json")
        assert usage_override.read_override("worker1", override_dir) is None

    def test_valid_entry_returned(self, override_dir):
        expires = time.time() + 100
        (override_dir / "worker1.json").write_text(
            json.dumps({"threshold": 0.90, "expires_at": expires})
        )
        result = usage_override.read_override("worker1", override_dir)
        assert result == {"threshold": 0.90, "expires_at": expires}

    def test_valid_unlimited_entry_returned(self, override_dir):
        expires = time.time() + 100
        (override_dir / "worker1.json").write_text(
            json.dumps({"threshold": None, "expires_at": expires})
        )
        result = usage_override.read_override("worker1", override_dir)
        assert result == {"threshold": None, "expires_at": expires}


class TestAdvanceOverride:
    def test_from_base_advances_to_90(self, override_dir):
        expires = time.time() + 100
        result = usage_override.advance_override("worker1", expires, override_dir)
        assert result["new_rung"] == 0.90
        assert result["already_unlimited"] is False
        on_disk = json.loads((override_dir / "worker1.json").read_text())
        assert on_disk["threshold"] == 0.90
        assert on_disk["expires_at"] == expires

    def test_from_90_advances_to_95(self, override_dir):
        expires = time.time() + 100
        (override_dir / "worker1.json").write_text(
            json.dumps({"threshold": 0.90, "expires_at": expires})
        )
        result = usage_override.advance_override("worker1", expires + 5, override_dir)
        assert result["new_rung"] == 0.95
        assert result["already_unlimited"] is False

    def test_from_95_advances_to_unlimited(self, override_dir):
        expires = time.time() + 100
        (override_dir / "worker1.json").write_text(
            json.dumps({"threshold": 0.95, "expires_at": expires})
        )
        result = usage_override.advance_override("worker1", expires + 5, override_dir)
        assert result["new_rung"] == "unlimited"
        assert result["already_unlimited"] is False
        on_disk = json.loads((override_dir / "worker1.json").read_text())
        assert on_disk["threshold"] is None

    def test_already_unlimited_reports_without_error(self, override_dir):
        expires = time.time() + 100
        (override_dir / "worker1.json").write_text(
            json.dumps({"threshold": None, "expires_at": expires})
        )
        result = usage_override.advance_override("worker1", expires + 5, override_dir)
        assert result["new_rung"] == "unlimited"
        assert result["already_unlimited"] is True

    def test_expired_override_treated_as_base(self, override_dir):
        (override_dir / "worker1.json").write_text(
            json.dumps({"threshold": 0.95, "expires_at": time.time() - 10})
        )
        expires = time.time() + 100
        result = usage_override.advance_override("worker1", expires, override_dir)
        assert result["new_rung"] == 0.90

    def test_write_is_atomic_no_tmp_files_left(self, override_dir):
        expires = time.time() + 100
        usage_override.advance_override("worker1", expires, override_dir)
        leftovers = list(override_dir.glob(".*.tmp-*"))
        assert leftovers == []


class TestClearOverride:
    def test_clear_existing_returns_true(self, override_dir):
        (override_dir / "worker1.json").write_text(
            json.dumps({"threshold": 0.90, "expires_at": time.time() + 100})
        )
        assert usage_override.clear_override("worker1", override_dir) is True
        assert not (override_dir / "worker1.json").exists()

    def test_clear_missing_returns_false(self, override_dir):
        assert usage_override.clear_override("worker1", override_dir) is False

    def test_clear_global_scope(self, override_dir):
        (override_dir / "global.json").write_text(
            json.dumps({"threshold": 0.90, "expires_at": time.time() + 100})
        )
        assert usage_override.clear_override("global", override_dir) is True


class TestResolveExpiresAt:
    def test_reads_resets_at_from_state_file(self, tmp_path):
        state_file = tmp_path / "state.json"
        resets_at = time.time() + 1234
        state_file.write_text(
            json.dumps({"rate_limits": {"five_hour": {"resets_at": resets_at}}})
        )
        result = usage_override.resolve_expires_at(str(state_file))
        assert result == resets_at

    def test_missing_state_file_falls_back_to_now_plus_5h(self, tmp_path):
        before = time.time()
        result = usage_override.resolve_expires_at(str(tmp_path / "nope.json"))
        after = time.time()
        assert before + 5 * 3600 - 1 <= result <= after + 5 * 3600 + 1

    def test_malformed_state_file_falls_back(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("not json")
        before = time.time()
        result = usage_override.resolve_expires_at(str(state_file))
        assert result >= before + 5 * 3600 - 1

    def test_missing_resets_at_falls_back(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"rate_limits": {"five_hour": {}}}))
        before = time.time()
        result = usage_override.resolve_expires_at(str(state_file))
        assert result >= before + 5 * 3600 - 1


class TestReadUsedPercentage:
    def test_reads_used_percentage(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text(
            json.dumps({"rate_limits": {"five_hour": {"used_percentage": 42.5}}})
        )
        assert usage_override.read_used_percentage(str(state_file)) == 42.5

    def test_missing_file_returns_none(self, tmp_path):
        assert usage_override.read_used_percentage(str(tmp_path / "nope.json")) is None

    def test_malformed_returns_none(self, tmp_path):
        state_file = tmp_path / "state.json"
        state_file.write_text("garbage")
        assert usage_override.read_used_percentage(str(state_file)) is None


class TestDefaultOverrideDir:
    def test_default_dir_is_under_home_maniple(self):
        d = usage_override.default_override_dir()
        assert str(d).endswith(".maniple/usage_override")


class TestInstallGlobalUsageGuard:
    def test_writes_hook_script_to_dest_dir(self, tmp_path):
        dest_dir = tmp_path / "hooks"
        result = usage_override.install_global_usage_guard(
            threshold=0.80, dest_dir=dest_dir
        )
        script_path = result["script_path"]
        assert script_path.exists()
        assert script_path.parent == dest_dir
        content = script_path.read_text()
        assert "usage-pause" in content.lower() or "usage_pause" in content.lower()

    def test_snippet_mentions_threshold_and_global_scope(self, tmp_path):
        dest_dir = tmp_path / "hooks"
        result = usage_override.install_global_usage_guard(
            threshold=0.80, dest_dir=dest_dir
        )
        snippet = result["snippet"]
        assert "0.8" in snippet
        assert "global" in snippet
        assert str(dest_dir) in snippet

    def test_default_threshold_is_080(self, tmp_path):
        dest_dir = tmp_path / "hooks"
        result = usage_override.install_global_usage_guard(dest_dir=dest_dir)
        assert "0.8" in result["snippet"]

    def test_does_not_touch_real_home(self, tmp_path, monkeypatch):
        # Sanity check: passing an explicit dest_dir never falls back to
        # Path.home() -- the installer must never touch ~/.claude for real
        # in tests.
        fake_home_hooks = tmp_path / "should_not_be_used"
        monkeypatch.setattr(
            usage_override.Path, "home", lambda: tmp_path / "fake_home"
        )
        dest_dir = tmp_path / "explicit_dest"
        usage_override.install_global_usage_guard(threshold=0.8, dest_dir=dest_dir)
        assert not fake_home_hooks.exists()
        assert (dest_dir / "usage-pause-global.py").exists()

    def test_rewrite_is_idempotent_content(self, tmp_path):
        dest_dir = tmp_path / "hooks"
        usage_override.install_global_usage_guard(threshold=0.8, dest_dir=dest_dir)
        usage_override.install_global_usage_guard(threshold=0.8, dest_dir=dest_dir)
        content = (dest_dir / "usage-pause-global.py").read_text()
        from maniple_mcp.usage_pause_hook import render_hook_script

        assert content == render_hook_script()
