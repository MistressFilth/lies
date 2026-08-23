"""Allow ``python -m lies.cli`` invocation.

Required for ``mcp up`` which re-execs the CLI via
``sys.executable -m lies.cli mcp _serve`` after detaching. The package
form (post Task 5) replaces the old ``cli.py`` module, so this
``__main__`` re-implements the trailing ``app()`` block that used to
live at the bottom of ``cli.py``.
"""

from lies.cli import app

if __name__ == "__main__":
    app()
