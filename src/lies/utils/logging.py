"""Centralized logfire + stdlib logging setup."""
from __future__ import annotations

import logging
import os

import logfire


def configure_logging() -> None:
    """Configure logfire if a token is available, else fall back to stdlib."""
    if os.environ.get("LOGFIRE_TOKEN"):
        logfire.configure()
        logfire.instrument_pydantic_ai()
    else:
        logging.basicConfig(
            level=os.environ.get("LIES_LOG_LEVEL", "INFO"),
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        )
