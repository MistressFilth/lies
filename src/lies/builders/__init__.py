"""Source-format builders (PDF, Sphinx, HTML, bespoke).

Builders convert fetched bytes into markdown. They never call an
LLM, never write outside their workspace, never talk to qmd.

Import the built-in implementations here so importing the package gives
callers a fully populated default registry in every process.
"""

from __future__ import annotations

from . import bespoke as _bespoke  # noqa: F401
from . import html as _html  # noqa: F401
from . import liquid as _liquid  # noqa: F401
from . import pdf as _pdf  # noqa: F401
from . import sphinx as _sphinx  # noqa: F401

__all__ = []
