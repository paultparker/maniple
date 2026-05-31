"""The worker settings file injects AskUserQuestion Pre/PostToolUse hooks."""

import json
from pathlib import Path

from maniple_mcp.iterm_utils import build_stop_hook_settings_file


def test_settings_file_has_stop_and_question_hooks():
    marker = "abc123"
    path = build_stop_hook_settings_file(marker)
    settings = json.loads(Path(path).read_text())
    hooks = settings["hooks"]

    # Stop hook unchanged
    assert hooks["Stop"][0]["hooks"][0]["command"] == f"echo [worker-done:{marker}]"

    # PreToolUse writes the marker for AskUserQuestion
    pre = hooks["PreToolUse"][0]
    assert pre["matcher"] == "AskUserQuestion"
    pre_cmd = pre["hooks"][0]["command"]
    assert f"{marker}.json" in pre_cmd
    assert "pending" in pre_cmd

    # PostToolUse deletes the marker for AskUserQuestion
    post = hooks["PostToolUse"][0]
    assert post["matcher"] == "AskUserQuestion"
    post_cmd = post["hooks"][0]["command"]
    assert f"{marker}.json" in post_cmd
