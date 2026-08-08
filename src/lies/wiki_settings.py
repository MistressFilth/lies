"""Per-wiki language resolution chain (``LIES_LANG`` > ``lies.toml`` > default).

Distinct from per-agent ``providers.toml`` (which lives at
``wiki.providers_path``). This module owns the per-wiki project
settings file at ``<config_root>/lies.toml``.

Resolution order (matches the spec chain):

1. ``LIES_LANG`` env var — if non-empty after ``.strip()``, return early.
2. ``$XDG_CONFIG_HOME/lies/<name>/lies.toml`` — parse ``[settings].lang``.
3. ``DEFAULT_LANGUAGE`` fallback.

Every failure mode is permissive: stderr warning + defaults. No typed
errors are raised from ``WikiSettings.load`` or ``resolve_language``.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING

import tomllib

if TYPE_CHECKING:
    from lies.collections.record import Collection
    from lies.wiki.wiki import Wiki


DEFAULT_LANGUAGE = "en"


@dataclass(frozen=True)
class WikiSettings:
    """Per-wiki settings resolved from ``LIES_LANG`` env + ``lies.toml``.

    Fields are always populated; missing values fall back to module-level
    defaults. Loaded lazily via :meth:`WikiSettings.load`.
    """

    language: str

    @classmethod
    def load(cls, wiki: Wiki) -> WikiSettings:
        """Resolve the effective wiki settings.

        Missing ``LIES_LANG`` and missing ``lies.toml`` → defaults, silent.
        Invalid ``LIES_LANG`` (empty/whitespace) → treated as unset, silent.
        Invalid ``lies.toml`` (unparseable, wrong type, empty value) →
        defaults + ``warnings.warn(UserWarning)``.
        """
        from lies.wiki.wiki import Wiki

        if not isinstance(wiki, Wiki):
            raise TypeError("WikiSettings.load requires a Wiki instance")

        # 1. Env wins (short-circuits the toml).
        env_lang = os.environ.get("LIES_LANG", "").strip()
        if env_lang:
            return cls(language=env_lang)

        # 2. Read lies.toml.
        path = wiki.settings_path
        if not path.exists():
            return cls(language=DEFAULT_LANGUAGE)

        try:
            with path.open("rb") as f:
                payload = tomllib.load(f)
        except tomllib.TOMLDecodeError:
            warnings.warn(
                f"lies.toml at {path} is not valid TOML; falling back to defaults",
                stacklevel=2,
            )
            return cls(language=DEFAULT_LANGUAGE)

        settings = payload.get("settings") if isinstance(payload, dict) else None
        if not isinstance(settings, dict):
            return cls(language=DEFAULT_LANGUAGE)

        lang = settings.get("lang")
        if lang is None:
            return cls(language=DEFAULT_LANGUAGE)
        if not isinstance(lang, str):
            warnings.warn(
                "lies.toml [settings].lang must be a string; falling back to defaults",
                stacklevel=2,
            )
            return cls(language=DEFAULT_LANGUAGE)
        stripped = lang.strip()
        if not stripped:
            warnings.warn(
                "lies.toml [settings].lang is empty; falling back to defaults",
                stacklevel=2,
            )
            return cls(language=DEFAULT_LANGUAGE)
        return cls(language=stripped)


def resolve_language(wiki: Wiki, collection: Collection | None = None) -> str:
    """Return the effective language for ``wiki``.

    When ``collection`` is provided AND its ``language`` field is set
    (i.e. non-None), the collection value wins. Otherwise the
    wiki-global (resolved from env > toml > default) is returned.
    """
    if collection is not None and collection.language is not None:
        return collection.language
    return WikiSettings.load(wiki).language
