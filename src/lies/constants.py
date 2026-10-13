"""Project-wide constants shared across the package.

Keep entries here ONLY when the value is referenced from two or more
modules. Single-site literals stay next to the code that uses them.
"""

from __future__ import annotations

# The per-tool subdirectory name inside every XDG role root. Single source of truth: a
# Path literal of the form ``<xdg_root>/"lies"/<name>`` should resolve to
# ``<xdg_root>/LIES_DATA_SUBDIR/<name>``, and the value stays ``lies`` until a
# coordinated migration rewrites every composed root.
LIES_DATA_SUBDIR = "lies"
