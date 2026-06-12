"""Import hygiene tests.

Verifies that maniple_mcp.tools can be imported in a fresh interpreter without
triggering the circular-import bug where utils/errors.py did a top-level import
of SessionRegistry/ManagedSession from registry.py while registry.py was still
being initialised.
"""

import subprocess
import sys


def test_tools_package_imports_without_circular_import():
    """Importing maniple_mcp.tools in a fresh subprocess must succeed (exit 0).

    This guards against the order-dependent circular import:
        tools/__init__.py → adopt_worker → registry
        registry → terminal_backends → utils/__init__ → errors
        errors → registry  (FAILS: registry is not fully initialised yet)
    """
    result = subprocess.run(
        [sys.executable, "-c", "import maniple_mcp.tools"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"import maniple_mcp.tools raised an error in a fresh interpreter:\n"
        f"STDOUT: {result.stdout}\n"
        f"STDERR: {result.stderr}"
    )
