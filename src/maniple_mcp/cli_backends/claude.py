"""
Claude Code CLI backend.

Implements the AgentCLI protocol for Claude Code CLI.
This preserves the existing behavior from iterm_utils.py.
"""

from typing import Literal

from .base import AgentCLI
from ..utils.env_vars import get_env_with_fallback

# Built-in default command.
_DEFAULT_COMMAND = "claude"

# Environment variables for command override (takes highest precedence).
_ENV_VAR = "MANIPLE_COMMAND"
_ENV_VAR_FALLBACK = "CLAUDE_TEAM_COMMAND"


def get_claude_command() -> str:
    """
    Get the Claude CLI command with precedence: env var > config > default.

    Resolution order:
    1. MANIPLE_COMMAND environment variable (for override)
    2. Config file commands.claude setting
    3. Built-in default "claude"

    Returns:
        The command to use for Claude CLI
    """
    # Environment variable takes highest precedence (for override).
    env_val = get_env_with_fallback(_ENV_VAR, _ENV_VAR_FALLBACK)
    if env_val:
        return env_val

    # Try config file next.
    # Import here to avoid circular imports and lazy-load config.
    try:
        from ..config import ConfigError, load_config

        config = load_config()
    except ConfigError:
        return _DEFAULT_COMMAND

    if config.commands.claude:
        return config.commands.claude

    # Fall back to built-in default.
    return _DEFAULT_COMMAND


class ClaudeCLI(AgentCLI):
    """
    Claude Code CLI implementation.

    Supports:
    - --dangerously-skip-permissions flag
    - --settings flag for Stop hook injection
    - Ready detection via TUI patterns (robot banner, '>' prompt, 'tokens' status)
    - Idle detection via Stop hook markers in JSONL
    """

    @property
    def engine_id(self) -> str:
        """Return 'claude' as the engine identifier."""
        return "claude"

    def command(self) -> str:
        """
        Return the Claude CLI command.

        Resolution order:
        1. MANIPLE_COMMAND environment variable (for override)
        2. Config file commands.claude setting
        3. Built-in default "claude"
        """
        return get_claude_command()

    def build_args(
        self,
        *,
        dangerously_skip_permissions: bool = False,
        settings_file: str | None = None,
        plugin_dir: str | list[str] | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> list[str]:
        """
        Build Claude CLI arguments.

        Args:
            dangerously_skip_permissions: Add --dangerously-skip-permissions
            settings_file: Path to settings JSON for Stop hook injection
            plugin_dir: Path(s) to plugin directory for --plugin-dir (single string or list)
            model: Optional model name to pass as --model <value>.
                Omitted entirely when None — do not pass an empty value.
            effort: Optional effort level to pass as --effort <value>.
                Omitted entirely when None — do not pass an empty value.

        Returns:
            List of CLI arguments
        """
        args: list[str] = []

        if dangerously_skip_permissions:
            args.append("--dangerously-skip-permissions")

        if plugin_dir:
            # Support both single string and list of strings
            plugin_dirs = [plugin_dir] if isinstance(plugin_dir, str) else plugin_dir
            for dir_path in plugin_dirs:
                args.append("--plugin-dir")
                args.append(dir_path)

        # Only add --settings for the default 'claude' command.
        # Custom commands like 'happy' have their own session tracking mechanisms.
        # See HAPPY_INTEGRATION_RESEARCH.md for full analysis.
        if settings_file and self._is_default_command():
            args.append("--settings")
            args.append(settings_file)

        # Append --model as an additional arg to the existing command.
        # MUST NOT inject model via commands.claude or MANIPLE_COMMAND — doing so
        # would disable the Stop-hook --settings injection (see _is_default_command).
        if model:
            args.append("--model")
            args.append(model)

        # Append --effort as an additional arg, same constraint as --model above:
        # MUST NOT inject via commands.claude or MANIPLE_COMMAND.
        if effort:
            args.append("--effort")
            args.append(effort)

        return args

    def ready_patterns(self) -> list[str]:
        """
        Return patterns indicating Claude TUI is ready.

        These patterns appear in Claude's startup:
        - '>' prompt indicates input ready
        - 'tokens' in status bar
        - Parts of the robot ASCII art banner
        """
        return [
            ">",  # Input prompt
            "tokens",  # Status bar
            "Claude Code v",  # Version line in banner
            "▐▛███▜▌",  # Top of robot head
            "▝▜█████▛▘",  # Middle of robot
        ]

    def idle_detection_method(self) -> Literal["stop_hook", "jsonl_stream", "none"]:
        """
        Claude uses Stop hook for idle detection.

        A Stop hook writes a marker to the JSONL when Claude finishes responding.
        """
        return "stop_hook"

    def supports_settings_file(self) -> bool:
        """
        Claude supports --settings for hook injection.

        Only returns True for the default 'claude' command.
        Custom wrappers may have their own settings mechanisms.
        """
        return self._is_default_command()

    def _is_default_command(self) -> bool:
        """Check if using the default 'claude' command (not a custom wrapper)."""
        return get_claude_command() == _DEFAULT_COMMAND


# Singleton instance for convenience
claude_cli = ClaudeCLI()
