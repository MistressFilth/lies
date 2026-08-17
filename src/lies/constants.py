"""Project-wide constants shared across the package.

Keep entries here ONLY when the value is referenced from two or more
modules. Single-site literals stay next to the code that uses them.
"""

from __future__ import annotations

#: The per-tool subdirectory name used inside every XDG role root
#: (``$XDG_DATA_HOME/lies/<name>``, ``$XDG_CONFIG_HOME/lies/<name>``, ...).
#: Used by :mod:`lies.wiki`, :mod:`lies.errors`, and the cli/mcp/migrate
#: paths that compose with XDG roots. Single source of truth — every
#: Path literal of the form ``<xdg_root>/"lies"/<name>`` should resolve
#: to ``<xdg_root>/LIES_DATA_SUBDIR/<name>``.
LIES_DATA_SUBDIR = "lies"
