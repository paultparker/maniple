"""Guard against import-order-dependent circular imports.

Each maniple_mcp module must be importable as the FIRST import in a fresh
interpreter. A circular import between utils/constants.py and
issue_tracker/__init__.py once made `import maniple_mcp.worker_prompt`
fail standalone (so `uv run pytest tests/test_worker_prompt.py` errored at
collection) while the full suite passed by import-order luck -- these
subprocess tests pin the fix.
"""

import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    [
        "maniple_mcp.issue_tracker",
        "maniple_mcp.utils",
        "maniple_mcp.worker_prompt",
        "maniple_mcp.server",
    ],
)
def test_module_imports_standalone(module):
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
