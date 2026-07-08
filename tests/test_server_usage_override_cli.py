"""Tests for the `maniple usage-override` / `maniple install-global-usage-guard`
CLI subcommands wired into server.py::main()'s argparse dispatch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import maniple_mcp.server as server_module
from maniple_mcp import config as config_module
from maniple_mcp import usage_override


@pytest.fixture(autouse=True)
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    return path


@pytest.fixture(autouse=True)
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # Both the global override dir and the hook installer default to
    # Path.home() -- redirect it so these tests never touch the real
    # ~/.maniple or ~/.claude directories.
    home = tmp_path / "fake_home"
    home.mkdir()
    monkeypatch.setattr(usage_override.Path, "home", lambda: home)
    return home


def _run_main(monkeypatch, argv):
    monkeypatch.setattr("sys.argv", ["maniple"] + argv)
    server_module.main()


class TestUsageOverrideSubcommand:
    def test_no_args_advances_global_rung(self, monkeypatch, capsys):
        _run_main(monkeypatch, ["usage-override"])
        out = capsys.readouterr().out
        assert "new rung: 0.9" in out
        assert "expires_at" in out

    def test_clear_removes_override(self, monkeypatch, capsys, fake_home):
        _run_main(monkeypatch, ["usage-override"])
        capsys.readouterr()
        _run_main(monkeypatch, ["usage-override", "--clear"])
        out = capsys.readouterr().out
        assert "cleared" in out
        override_path = fake_home / ".maniple" / "usage_override" / "global.json"
        assert not override_path.exists()

    def test_clear_with_no_override_reports_none(self, monkeypatch, capsys):
        _run_main(monkeypatch, ["usage-override", "--clear"])
        out = capsys.readouterr().out
        assert "no override was set" in out

    def test_status_reports_base_rung_initially(self, monkeypatch, capsys):
        _run_main(monkeypatch, ["usage-override", "--status"])
        out = capsys.readouterr().out
        assert "rung: base" in out

    def test_status_reports_rung_after_advance(self, monkeypatch, capsys):
        _run_main(monkeypatch, ["usage-override"])
        capsys.readouterr()
        _run_main(monkeypatch, ["usage-override", "--status"])
        out = capsys.readouterr().out
        assert "rung: 0.9" in out

    def test_status_and_clear_are_mutually_exclusive(self, monkeypatch):
        monkeypatch.setattr(
            "sys.argv", ["maniple", "usage-override", "--status", "--clear"]
        )
        with pytest.raises(SystemExit):
            server_module.main()


class TestInstallGlobalUsageGuardSubcommand:
    def test_writes_hook_script_and_prints_snippet(
        self, monkeypatch, capsys, fake_home
    ):
        _run_main(monkeypatch, ["install-global-usage-guard"])
        out = capsys.readouterr().out
        script_path = fake_home / ".claude" / "hooks" / "usage-pause-global.py"
        assert script_path.exists()
        assert str(script_path) in out
        assert "PreToolUse" in out or "hooks" in out
        assert "global" in out

    def test_never_touches_settings_json(self, monkeypatch, capsys, fake_home):
        _run_main(monkeypatch, ["install-global-usage-guard"])
        capsys.readouterr()
        settings_path = fake_home / ".claude" / "settings.json"
        assert not settings_path.exists()

    def test_custom_threshold_reflected_in_snippet(self, monkeypatch, capsys):
        _run_main(monkeypatch, ["install-global-usage-guard", "--threshold", "0.65"])
        out = capsys.readouterr().out
        assert "0.65" in out

    def test_threshold_of_80_rejected_not_silently_8000_percent(
        self, monkeypatch, capsys
    ):
        """`--threshold 80` (a plausible typo for "80%") must be rejected,
        not silently accepted as a nonsensical 8000% threshold -- mirrors
        config.py's context_pause/usage_pause threshold validation, which
        already requires 0 < t < 1."""
        with pytest.raises(SystemExit):
            _run_main(monkeypatch, ["install-global-usage-guard", "--threshold", "80"])
        err = capsys.readouterr().err
        assert "threshold" in err.lower()

    def test_threshold_zero_rejected(self, monkeypatch, capsys):
        with pytest.raises(SystemExit):
            _run_main(monkeypatch, ["install-global-usage-guard", "--threshold", "0"])

    def test_threshold_one_rejected(self, monkeypatch, capsys):
        with pytest.raises(SystemExit):
            _run_main(monkeypatch, ["install-global-usage-guard", "--threshold", "1"])

    def test_threshold_negative_rejected(self, monkeypatch, capsys):
        with pytest.raises(SystemExit):
            _run_main(monkeypatch, ["install-global-usage-guard", "--threshold", "-0.1"])

    def test_threshold_within_range_still_accepted(self, monkeypatch, capsys, fake_home):
        _run_main(monkeypatch, ["install-global-usage-guard", "--threshold", "0.5"])
        out = capsys.readouterr().out
        assert "0.5" in out
