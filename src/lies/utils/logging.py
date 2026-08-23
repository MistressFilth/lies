"""Centralized logfire + stdlib logging setup.

``logfire`` is imported lazily so the bare CLI (e.g. ``lies --help``)
does not pull in its ~160ms of transitive deps (opentelemetry, requests,
markdown_it, attr, ...). Only commands that actually configure logging
pay that cost.
"""

from __future__ import annotations

import logging
import os


def configure_logging() -> None:
    """Configure logfire if a token is available, else fall back to stdlib."""
    if os.environ.get("LOGFIRE_TOKEN"):
        import logfire

        logfire.configure()
        logfire.instrument_pydantic_ai()
    else:
        logging.basicConfig(
            level=os.environ.get("LIES_LOG_LEVEL", "INFO"),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
