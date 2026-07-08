"""Tests for config CLI helpers."""

import json
from pathlib import Path

import pytest

from maniple_mcp import config as config_module
from maniple_mcp.config import ConfigError, load_config
from maniple_mcp.config_cli import (
    get_config_value,
    init_config,
    load_effective_config_data,
    render_config_json,
    set_config_value,
)


@pytest.fixture(autouse=True)
def config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config path to a temp location for deterministic tests."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_PATH", path)
    return path


class TestConfigInit:
    """Tests for config init helper."""

    def test_init_creates_default(self, config_path: Path):
        """init_config writes the default config file."""
        result = init_config()
        assert result == config_path
        assert config_path.exists()

    def test_init_errors_when_exists(self, config_path: Path):
        """init_config errors without --force if file exists."""
        config_path.write_text("{}")
        with pytest.raises(ConfigError, match="already exists"):
            init_config()


class TestConfigShow:
    """Tests for config show helper."""

    def test_show_merges_env_overrides(self, config_path: Path):
        """render_config_json applies env overrides on top of config."""
        config_path.write_text(json.dumps({
            "version": 1,
            "commands": {"claude": "/from/config"},
            "events": {"max_size_mb": 2},
        }))
        env = {
            "MANIPLE_COMMAND": "from-env",
            "MANIPLE_EVENTS_MAX_SIZE_MB": "5",
        }
        data = load_effective_config_data(env=env)
        assert data["commands"]["claude"] == "from-env"
        assert data["events"]["max_size_mb"] == 5

    def test_show_renders_json(self, config_path: Path):
        """render_config_json returns formatted JSON."""
        config_path.write_text(json.dumps({"version": 1}))
        payload = render_config_json()
        parsed = json.loads(payload)
        assert parsed["version"] == 1


class TestConfigGet:
    """Tests for config get helper."""

    def test_get_reads_dotted_value(self, config_path: Path):
        """get_config_value returns values by dotted path."""
        config_path.write_text(json.dumps({
            "version": 1,
            "defaults": {"layout": "new"},
        }))
        assert get_config_value("defaults.layout") == "new"

    def test_get_reads_env_override(self, config_path: Path):
        """get_config_value returns env-overridden values."""
        config_path.write_text(json.dumps({"version": 1}))
        env = {"MANIPLE_TERMINAL_BACKEND": "tmux"}
        assert get_config_value("terminal.backend", env=env) == "tmux"


class TestConfigSet:
    """Tests for config set helper."""

    def test_set_creates_file_and_saves(self, config_path: Path):
        """set_config_value creates file and persists updates."""
        set_config_value("defaults.skip_permissions", "true")
        config = load_config()
        assert config.defaults.skip_permissions is True

    def test_set_validates_values(self, config_path: Path):
        """set_config_value validates against the schema."""
        with pytest.raises(ConfigError, match="defaults.layout must be one of"):
            set_config_value("defaults.layout", "grid")

    def test_set_rejects_unknown_key(self, config_path: Path):
        """set_config_value rejects unknown keys."""
        with pytest.raises(ConfigError, match="Unknown config key"):
            set_config_value("defaults.unknown", "true")

    def test_set_stale_threshold_minutes(self, config_path: Path):
        """set_config_value persists stale_threshold_minutes."""
        set_config_value("events.stale_threshold_minutes", "30")
        config = load_config()
        assert config.events.stale_threshold_minutes == 30

    def test_set_context_pause_enabled(self, config_path: Path):
        """set_config_value persists context_pause.enabled."""
        set_config_value("context_pause.enabled", "false")
        config = load_config()
        assert config.context_pause.enabled is False

    def test_set_context_pause_threshold(self, config_path: Path):
        """set_config_value persists context_pause.threshold."""
        set_config_value("context_pause.threshold", "0.6")
        config = load_config()
        assert config.context_pause.threshold == 0.6

    def test_set_context_pause_threshold_out_of_range_rejected(self, config_path: Path):
        """set_config_value rejects an out-of-range threshold."""
        with pytest.raises(ConfigError, match="context_pause.threshold must be"):
            set_config_value("context_pause.threshold", "1.5")

    def test_set_context_pause_window_tokens(self, config_path: Path):
        """set_config_value persists context_pause.window_tokens."""
        set_config_value("context_pause.window_tokens", "50000")
        config = load_config()
        assert config.context_pause.window_tokens == 50000

    def test_set_context_pause_max_tokens(self, config_path: Path):
        """set_config_value persists context_pause.max_tokens."""
        set_config_value("context_pause.max_tokens", "100000")
        config = load_config()
        assert config.context_pause.max_tokens == 100000

    def test_set_context_pause_max_tokens_below_minimum_rejected(self, config_path: Path):
        """set_config_value rejects a max_tokens below the minimum."""
        with pytest.raises(ConfigError, match="context_pause.max_tokens must be at least"):
            set_config_value("context_pause.max_tokens", "999")

    def test_set_usage_pause_enabled(self, config_path: Path):
        """set_config_value persists usage_pause.enabled."""
        set_config_value("usage_pause.enabled", "false")
        config = load_config()
        assert config.usage_pause.enabled is False

    def test_set_usage_pause_threshold(self, config_path: Path):
        """set_config_value persists usage_pause.threshold."""
        set_config_value("usage_pause.threshold", "0.6")
        config = load_config()
        assert config.usage_pause.threshold == 0.6

    def test_set_usage_pause_threshold_out_of_range_rejected(self, config_path: Path):
        """set_config_value rejects an out-of-range threshold."""
        with pytest.raises(ConfigError, match="usage_pause.threshold must be"):
            set_config_value("usage_pause.threshold", "1.5")

    def test_set_usage_pause_state_file(self, config_path: Path):
        """set_config_value persists usage_pause.state_file."""
        set_config_value("usage_pause.state_file", "/tmp/other.json")
        config = load_config()
        assert config.usage_pause.state_file == "/tmp/other.json"

    def test_set_usage_pause_state_file_empty_rejected(self, config_path: Path):
        """set_config_value rejects an empty state_file."""
        with pytest.raises(ConfigError, match="usage_pause.state_file cannot be empty"):
            set_config_value("usage_pause.state_file", "")

    def test_set_usage_pause_max_stale_seconds(self, config_path: Path):
        """set_config_value persists usage_pause.max_stale_seconds."""
        set_config_value("usage_pause.max_stale_seconds", "120")
        config = load_config()
        assert config.usage_pause.max_stale_seconds == 120

    def test_get_usage_pause_threshold(self, config_path: Path):
        """get_config_value returns usage_pause.threshold."""
        config_path.write_text(json.dumps({
            "version": 1,
            "usage_pause": {"threshold": 0.6},
        }))
        assert get_config_value("usage_pause.threshold") == 0.6

    def test_get_context_pause_threshold(self, config_path: Path):
        """get_config_value returns context_pause.threshold."""
        config_path.write_text(json.dumps({
            "version": 1,
            "context_pause": {"threshold": 0.6},
        }))
        assert get_config_value("context_pause.threshold") == 0.6

    def test_get_context_pause_max_tokens(self, config_path: Path):
        """get_config_value returns context_pause.max_tokens."""
        config_path.write_text(json.dumps({
            "version": 1,
            "context_pause": {"max_tokens": 100000},
        }))
        assert get_config_value("context_pause.max_tokens") == 100000


class TestStaleThresholdEnvOverride:
    """Tests for stale_threshold_minutes env override."""

    def test_env_overrides_config(self, config_path: Path):
        """MANIPLE_STALE_THRESHOLD_MINUTES overrides file config."""
        config_path.write_text(json.dumps({
            "version": 1,
            "events": {"stale_threshold_minutes": 15},
        }))
        env = {"MANIPLE_STALE_THRESHOLD_MINUTES": "25"}
        data = load_effective_config_data(env=env)
        assert data["events"]["stale_threshold_minutes"] == 25

    def test_env_overrides_default(self, config_path: Path):
        """MANIPLE_STALE_THRESHOLD_MINUTES overrides default when no file."""
        env = {"MANIPLE_STALE_THRESHOLD_MINUTES": "5"}
        data = load_effective_config_data(env=env)
        assert data["events"]["stale_threshold_minutes"] == 5

    def test_invalid_env_value_ignored(self, config_path: Path):
        """Non-integer env value is silently ignored."""
        config_path.write_text(json.dumps({
            "version": 1,
            "events": {"stale_threshold_minutes": 15},
        }))
        env = {"MANIPLE_STALE_THRESHOLD_MINUTES": "not_a_number"}
        data = load_effective_config_data(env=env)
        assert data["events"]["stale_threshold_minutes"] == 15

    def test_deprecated_env_fallback(self, config_path: Path):
        """Deprecated CLAUDE_TEAM_STALE_THRESHOLD_MINUTES is still honored."""
        env = {"CLAUDE_TEAM_STALE_THRESHOLD_MINUTES": "7"}
        data = load_effective_config_data(env=env)
        assert data["events"]["stale_threshold_minutes"] == 7

    def test_env_precedence(self, config_path: Path):
        """MANIPLE_STALE_THRESHOLD_MINUTES takes precedence over deprecated env var."""
        env = {"MANIPLE_STALE_THRESHOLD_MINUTES": "9", "CLAUDE_TEAM_STALE_THRESHOLD_MINUTES": "7"}
        data = load_effective_config_data(env=env)
        assert data["events"]["stale_threshold_minutes"] == 9
