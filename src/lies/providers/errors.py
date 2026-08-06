"""Exceptions raised by the providers subsystem."""

from __future__ import annotations


class ProviderConfigError(Exception):
    """Raised when providers.toml is missing, malformed, or references unknown entities."""
