"""Per-wiki language resolution chain (``LIES_LANG`` > ``lies.toml`` > default).

Distinct from per-agent ``providers.toml`` (which lives at
``wiki.providers_path``). This module owns the per-wiki project
settings file at ``<config_root>/lies.toml``.

Resolution order (matches the spec chain):

1. ``LIES_LANG`` env var — if non-empty after ``.strip()``, return early.
2. ``$XDG_CONFIG_HOME/lies/<name>/lies.toml`` — parse ``[settings].lang``.
3. ``DEFAULT_LANGUAGE`` fallback.

``[settings].version`` is parsed alongside ``lang`` and exposed as
:attr:`WikiSettings.settings_version`. When the stored value differs
from :data:`CURRENT_SETTINGS_VERSION`, a ``UserWarning`` is emitted
advising the user to check release notes for migration guidance.
Missing, empty, or non-string ``version`` values each emit a warning
and surface as ``settings_version=None``; the load never raises.

Every failure mode is permissive: stderr warning + defaults. No typed
errors are raised from ``WikiSettings.load`` or ``resolve_language``.
"""

from __future__ import annotations

import os
import tomllib
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lies.library.record import LibraryCollectionConfig as Collection
    from lies.wiki.wiki import Wiki


DEFAULT_LANGUAGE = "en"

# Bumped when the ``[settings]`` schema in ``lies.toml`` changes
# incompatibly. Older pinned values trigger a ``UserWarning`` at load
# time; the load itself never refuses.
CURRENT_SETTINGS_VERSION = "1"


@dataclass(frozen=True)
class WikiSettings:
    """Per-wiki settings resolved from ``LIES_LANG`` env + ``lies.toml``.

    Fields are always populated; missing values fall back to module-level
    defaults. Loaded lazily via :meth:`WikiSettings.load`.

    Attributes:
        language: Effective wiki language (resolved ``LIES_LANG`` >
            ``[settings].lang`` > ``DEFAULT_LANGUAGE``).
        settings_version: ``[settings].version`` from ``lies.toml``, or
            ``None`` when the file is absent, the field is missing, or
            the value is empty/non-string. A non-``None`` value that
            differs from :data:`CURRENT_SETTINGS_VERSION` triggers a
            warning at load time.
    """

    language: str
    settings_version: str | None = None

    @classmethod
    def load(cls, wiki: Wiki) -> WikiSettings:
        """Resolve the effective wiki settings.

        Missing ``LIES_LANG`` and missing ``lies.toml`` → defaults, silent.
        Invalid ``LIES_LANG`` (empty/whitespace) → treated as unset, silent.
        Invalid ``lies.toml`` (unparseable, wrong type, empty value) →
        defaults + ``warnings.warn(UserWarning)``.
        """
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

        # [settings].version — parsed alongside lang, never raises.
        settings_version = _parse_settings_version(settings, path)

        return cls(language=stripped, settings_version=settings_version)


def _parse_settings_version(settings: dict, path: Path) -> str | None:
    """Resolve and validate ``[settings].version``.

    Returns the stripped value when it parses as a non-empty string,
    or ``None`` when missing/empty/non-string. Emits a ``UserWarning``
    on type errors, empty values, and version mismatches against
    :data:`CURRENT_SETTINGS_VERSION`. Never raises.
    """
    version = settings.get("version") if isinstance(settings, dict) else None
    if version is not None and not isinstance(version, str):
        warnings.warn(
            "lies.toml [settings].version must be a string; ignoring",
            stacklevel=2,
        )
        return None
    if not isinstance(version, str):
        return None
    stripped_version = version.strip()
    if not stripped_version:
        warnings.warn(
            "lies.toml [settings].version is empty; ignoring",
            stacklevel=2,
        )
        return None
    if stripped_version != CURRENT_SETTINGS_VERSION:
        warnings.warn(
            f"lies.toml at {path} has settings.version={stripped_version!r} "
            f"but current is {CURRENT_SETTINGS_VERSION!r}; "
            "check release notes for migration guidance",
            stacklevel=2,
        )
    return stripped_version


def resolve_language(wiki: Wiki, collection: Collection | None = None) -> str:
    """Return the effective language for ``wiki``.

    When ``collection`` is provided AND its ``language`` field is set
    (i.e. non-None), the collection value wins. Otherwise the
    wiki-global (resolved from env > toml > default) is returned.
    """
    if collection is not None and collection.language is not None:
        return collection.language
    return WikiSettings.load(wiki).language
