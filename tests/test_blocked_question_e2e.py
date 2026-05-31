"""End-to-end: a real worker blocks on AskUserQuestion; detector + answer work.

Opt-in (spawns a real claude session in tmux). Run explicitly:
    uv run pytest tests/test_blocked_question_e2e.py -m e2e -q
Requires: tmux, the `claude` CLI on PATH, and a logged-in Claude account.
"""

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from maniple_mcp.session_state import find_pending_question

pytestmark = pytest.mark.e2e

SESSION = "maniple_e2e_test"
PROMPT = (
    "Using the AskUserQuestion tool, ask me to choose a logging level: "
    "INFO or DEBUG. Do this as your very first action and nothing else."
)


def _tmux(*args: str) -> str:
    return subprocess.run(["tmux", *args], capture_output=True, text=True).stdout


def _capture() -> str:
    return _tmux("capture-pane", "-p", "-t", SESSION)


def _newest_jsonl_since(since: float) -> Path | None:
    root = Path.home() / ".claude" / "projects"
    if not root.exists():
        return None
    candidates = [
        p for p in root.glob("**/*.jsonl")
        if p.stat().st_mtime >= since
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


@pytest.fixture
def worker_dir(tmp_path):
    d = tmp_path / "proj"
    d.mkdir()
    yield d


@pytest.mark.skipif(not shutil.which("claude"), reason="claude CLI not installed")
@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not installed")
def test_detect_and_answer_real_worker(worker_dir):
    _tmux("kill-session", "-t", SESSION)  # ignore failure if absent
    launch_ts = time.time() - 1  # small skew margin
    _tmux("new-session", "-d", "-s", SESSION, "-x", "200", "-y", "50",
          "-c", str(worker_dir))
    try:
        _tmux("send-keys", "-t", SESSION, f'claude "{PROMPT}"', "Enter")

        # Wait for the menu to render.
        deadline = time.time() + 90
        while time.time() < deadline and "Enter to select" not in _capture():
            time.sleep(2)
        assert "Enter to select" in _capture(), "worker never showed the menu"

        # Find the worker's JSONL and assert the detector finds the question.
        deadline = time.time() + 30
        q = None
        jsonl = None
        while time.time() < deadline:
            jsonl = _newest_jsonl_since(launch_ts)
            if jsonl:
                q = find_pending_question(jsonl)
                if q:
                    break
            time.sleep(2)
        assert jsonl is not None, "could not locate worker JSONL"
        assert q is not None and q["answerable"] is True
        labels = [o["label"] for o in q["options"]]
        assert "INFO" in labels and "DEBUG" in labels

        # Answer by sending the option number (hotkey selects + submits).
        idx = labels.index("DEBUG") + 1
        _tmux("send-keys", "-t", SESSION, "-l", str(idx))
        time.sleep(5)

        pane = _capture()
        assert "DEBUG" in pane and "Enter to select" not in pane
        assert find_pending_question(jsonl) is None  # no longer pending
    finally:
        _tmux("kill-session", "-t", SESSION)
