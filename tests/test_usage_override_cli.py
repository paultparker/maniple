"""Tests for src/maniple_mcp/usage_override_cli.py -- the CLI helper layer
behind `maniple usage-override`, scoped to the "global" override.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from maniple_mcp import config as config_module
from maniple_mcp import usage_override_cli


@pytest.fixture(autouse=True)
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config path to a temp location for deterministic tests."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    return path


@pytest.fixture()
def override_dir(tmp_path):
    d = tmp_path / "usage_override"
    d.mkdir()
    return d


class TestAdvanceGlobal:
    def test_from_base_advances_to_90(self, override_dir):
        result = usage_override_cli.advance_global(override_dir=override_dir)
        assert result["new_rung"] == 0.90
        assert result["already_unlimited"] is False
        assert (override_dir / "global.json").exists()

    def test_writes_scope_global(self, override_dir):
        usage_override_cli.advance_global(override_dir=override_dir)
        data = json.loads((override_dir / "global.json").read_text())
        assert data["threshold"] == 0.90

    def test_repeated_calls_climb_the_ladder(self, override_dir):
        usage_override_cli.advance_global(override_dir=override_dir)
        second = usage_override_cli.advance_global(override_dir=override_dir)
        assert second["new_rung"] == 0.95

    def test_missing_config_falls_back_gracefully(
        self, override_dir, tmp_path, config_path
    ):
        # No usage_pause.state_file configured -- must not raise, expires_at
        # falls back to now + 5h via resolve_expires_at's own fallback path.
        # Point state_file at a nonexistent temp path rather than relying on
        # the real default (/tmp/cc-statusline-input.json), which may or may
        # not exist on the machine running the tests.
        config_path.write_text(
            json.dumps(
                {"usage_pause": {"state_file": str(tmp_path / "does-not-exist.json")}}
            )
        )
        result = usage_override_cli.advance_global(override_dir=override_dir)
        assert result["expires_at"] > time.time()


class TestClearGlobal:
    def test_clear_existing_returns_true(self, override_dir):
        usage_override_cli.advance_global(override_dir=override_dir)
        assert usage_override_cli.clear_global(override_dir=override_dir) is True
        assert not (override_dir / "global.json").exists()

    def test_clear_missing_returns_false(self, override_dir):
        assert usage_override_cli.clear_global(override_dir=override_dir) is False


class TestStatusGlobal:
    def test_status_with_no_override_reports_base(self, override_dir):
        status = usage_override_cli.status_global(override_dir=override_dir)
        assert status["rung"] == "base"
        assert status["expires_at"] is None

    def test_status_after_advance_reports_rung_and_expiry(
        self, override_dir, tmp_path, config_path
    ):
        config_path.write_text(
            json.dumps(
                {"usage_pause": {"state_file": str(tmp_path / "does-not-exist.json")}}
            )
        )
        usage_override_cli.advance_global(override_dir=override_dir)
        status = usage_override_cli.status_global(override_dir=override_dir)
        assert status["rung"] == 0.90
        assert status["expires_at"] is not None

    def test_status_unlimited_rung_reported_as_unlimited(
        self, override_dir, tmp_path, config_path
    ):
        config_path.write_text(
            json.dumps(
                {"usage_pause": {"state_file": str(tmp_path / "does-not-exist.json")}}
            )
        )
        usage_override_cli.advance_global(override_dir=override_dir)
        usage_override_cli.advance_global(override_dir=override_dir)
        usage_override_cli.advance_global(override_dir=override_dir)
        status = usage_override_cli.status_global(override_dir=override_dir)
        assert status["rung"] == "unlimited"

    def test_status_reads_used_percentage_from_configured_state_file(
        self, override_dir, tmp_path, config_path
    ):
        state_file = tmp_path / "statusline.json"
        state_file.write_text(
            json.dumps({"rate_limits": {"five_hour": {"used_percentage": 42.5}}})
        )
        config_path.write_text(
            json.dumps({"usage_pause": {"state_file": str(state_file)}})
        )
        status = usage_override_cli.status_global(override_dir=override_dir)
        assert status["used_percentage"] == 42.5

    def test_status_missing_state_file_reports_none_used_percentage(
        self, override_dir, tmp_path, config_path
    ):
        config_path.write_text(
            json.dumps(
                {"usage_pause": {"state_file": str(tmp_path / "does-not-exist.json")}}
            )
        )
        status = usage_override_cli.status_global(override_dir=override_dir)
        assert status["used_percentage"] is None
