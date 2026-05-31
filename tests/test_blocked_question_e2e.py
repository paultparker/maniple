"""End-to-end: real worker blocks on AskUserQuestion; hook marker + answer work.

Opt-in (spawns a real claude session in tmux). Run: uv run pytest tests/test_blocked_question_e2e.py -m e2e -q
Requires tmux, the claude CLI, and a logged-in account.
"""
import shutil, subprocess, time
from pathlib import Path
import pytest
from maniple_mcp.iterm_utils import build_stop_hook_settings_file
from maniple_mcp.session_state import find_pending_question, PENDING_DIR

pytestmark = pytest.mark.e2e
SESSION = "maniple_e2e_test"
MARKER = "e2e-marker-test-001"
PROMPT = ("Using the AskUserQuestion tool, ask me to choose a logging level: INFO or DEBUG. "
          "Do this as your very first action and nothing else.")

def _tmux(*a): return subprocess.run(["tmux", *a], capture_output=True, text=True).stdout
def _cap(): return _tmux("capture-pane", "-p", "-t", SESSION)

@pytest.mark.skipif(not shutil.which("claude"), reason="claude not installed")
@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_detect_and_answer_real_worker(tmp_path):
    marker_file = PENDING_DIR / f"{MARKER}.json"
    marker_file.unlink(missing_ok=True)
    settings = build_stop_hook_settings_file(MARKER)
    _tmux("kill-session", "-t", SESSION)
    wdir = tmp_path / "proj"; wdir.mkdir()
    _tmux("new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50", "-c", str(wdir))
    try:
        _tmux("send-keys", "-t", SESSION, "-l", f'claude --settings {settings} "{PROMPT}"')
        _tmux("send-keys", "-t", SESSION, "Enter")
        # wait for the menu, dismissing a folder-trust prompt if it appears
        deadline = time.time() + 90
        while time.time() < deadline and "Enter to select" not in _cap():
            if "trust" in _cap().lower() and "Enter to select" not in _cap():
                _tmux("send-keys", "-t", SESSION, "Enter")
            time.sleep(2)
        assert "Enter to select" in _cap(), "worker never showed the menu"
        # PreToolUse hook should have written the marker WHILE pending
        deadline = time.time() + 20
        while time.time() < deadline and not marker_file.exists():
            time.sleep(1)
        assert marker_file.exists(), "PreToolUse hook did not write the pending marker"
        q = find_pending_question(MARKER)
        assert q is not None and q["answerable"] is True
        labels = [o["label"] for o in q["options"]]
        assert "INFO" in labels and "DEBUG" in labels
        # answer by sending the option number (hotkey selects+submits)
        idx = labels.index("DEBUG") + 1
        _tmux("send-keys", "-t", SESSION, "-l", str(idx))
        time.sleep(6)
        pane = _cap()
        assert "DEBUG" in pane and "Enter to select" not in pane
        # PostToolUse hook should have deleted the marker on answer
        deadline = time.time() + 15
        while time.time() < deadline and marker_file.exists():
            time.sleep(1)
        assert not marker_file.exists(), "PostToolUse hook did not delete the marker"
        assert find_pending_question(MARKER) is None
    finally:
        _tmux("kill-session", "-t", SESSION)
        marker_file.unlink(missing_ok=True)
