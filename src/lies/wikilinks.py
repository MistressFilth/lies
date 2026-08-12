"""WikiLink extraction and resolution.

Distinct from QMD (semantic search). Resolves ``[[WikiLink]]`` targets
against an in-memory dict of wiki-page identifiers (filename stem,
frontmatter title, frontmatter aliases) and detects broken links.

When the optional ``ahocorasick_rs`` runtime dep is present (default on
Python 3.14+), ``WikiLinkResolver.build()`` also constructs a
trie-based substring automaton and stores it on ``_aho``. ``resolve()``
then routes through ``find_matches_as_strings(..., overlapping=True)``
so prefix-overlapping keys (``foo`` + ``foobar``) still surface the
longest match. When the dep is missing, ``_aho`` stays ``None`` and
the dict branch alone runs — bit-identical output for every key.
"""

from __future__ import annotations

import logging
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import frontmatter  # type: ignore[import-untyped]

log = logging.getLogger(__name__)


# --- Errors ---------------------------------------------------------------


class WikiLinkError(Exception):
    """Base class for all wikilink errors."""


class WikiLinkCorpusMissing(WikiLinkError):
    """Raised when neither wiki/ nor raw/ exists under the wiki root."""


class WikiLinkFrontmatterUnparseable(WikiLinkError):
    """Raised when a page's frontmatter cannot be parsed as YAML."""

    def __init__(self, page: Path) -> None:
        super().__init__(f"frontmatter unparseable in {page}")
        self.page = page


# --- Extraction -----------------------------------------------------------

_WIKILINK_RE = re.compile(
    r"(?<!!)"
    r"\[\["
    r"(?P<target>[^\[\]\|#]+?)"  # target: no brackets, no |, no #
    r"(?:\|[^\[\]]*?)?"  # optional |alias (display; discarded)
    r"(?:#[^\[\]]*?)?"  # optional #anchor (display; discarded)
    r"\]\]"
)

_FENCED_CODE_RE = re.compile(
    r"(?P<fence>```|~~~)[^\n]*\n.*?^(?P=fence)[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+?`")

_EXCLUDED_DIRS = (".lies", ".git", "node_modules")


def _strip_code_spans(text: str) -> str:
    """Strip fenced + inline code spans so wikilinks inside them aren't matched."""
    text = _FENCED_CODE_RE.sub("", text)
    text = _INLINE_CODE_RE.sub("", text)
    return text


def extract_wikilinks(text: str) -> list[str]:
    """Return the raw ``[[target]]`` strings in ``text``, code spans excluded."""
    cleaned = _strip_code_spans(text)
    return [m.group("target") for m in _WIKILINK_RE.finditer(cleaned)]


# --- Page + corpus --------------------------------------------------------


@dataclass(frozen=True)
class PageKey:
    """One corpus entry: every form under which a wiki page resolves."""

    path: Path
    basenames: tuple[str, ...]


def _is_excluded(path: Path) -> bool:
    return any(part in _EXCLUDED_DIRS for part in path.parts)


def _collect_keys(fm: Any, stem: str) -> tuple[str, ...]:
    """Return lowercased, unique keys derived from a page's filename and frontmatter."""
    keys: set[str] = {stem.lower()}
    title = fm.get("title") if isinstance(fm, dict) else None
    if isinstance(title, str) and title.strip():
        keys.add(title.strip().lower())
    aliases = fm.get("aliases") if isinstance(fm, dict) else None
    if isinstance(aliases, list):
        for a in aliases:
            if isinstance(a, str) and a.strip():
                keys.add(a.strip().lower())
    alias = fm.get("alias") if isinstance(fm, dict) else None
    if isinstance(alias, str) and alias.strip():
        keys.add(alias.strip().lower())
    return tuple(sorted(keys))


# --- Resolver -------------------------------------------------------------


class WikiLinkResolver:
    """Dict-backed ``[[WikiLink]]`` resolver with optional Aho-Corasick fast-path.

    The AC automaton, when ``ahocorasick_rs`` is installed, lets
    ``resolve()`` scan a haystack for any corpus-key substring in
    O(|haystack|) instead of O(|corpus| × |haystack|) dict lookups.
    ``overlapping=True`` is required so prefix-overlapping keys
    (``foo`` + ``foobar``) still surface the longer match — the same
    longest-wins guarantee the dict branch pins in the test suite.
    """

    _keys: dict[str, Path]
    _aho: Any  # ahocorasick_rs.AhoCorasick | None when the dep is missing

    def __init__(self) -> None:
        self._keys = {}
        self._aho = None

    @classmethod
    def build(cls, roots: tuple[Path, ...]) -> WikiLinkResolver:
        """Walk ``roots`` for ``*.md``, parse frontmatter, build the corpus."""
        # Discover all pages.
        pages: list[PageKey] = []
        for root in roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*.md")):
                if _is_excluded(path):
                    continue
                try:
                    fm_obj = frontmatter.loads(path.read_text(encoding="utf-8"))
                except Exception as exc:  # noqa: BLE001
                    warnings.warn(
                        f"wikilink: frontmatter unparseable in {path}; skipping key extraction",
                        stacklevel=2,
                    )
                    fm: Any = {}
                    log.debug("frontmatter parse failed for %s: %s", path, exc)
                else:
                    fm = dict(fm_obj.metadata)
                keys = _collect_keys(fm, path.stem)
                pages.append(PageKey(path=path.resolve(), basenames=keys))

        if not pages and not any(r.exists() for r in roots):
            raise WikiLinkCorpusMissing(
                f"neither {roots[0]} nor {roots[1] if len(roots) > 1 else '<none>'} exists"
            )

        # Build the dict.
        resolver = cls()
        for page in pages:
            for key in page.basenames:
                if key in resolver._keys and resolver._keys[key] != page.path:
                    warnings.warn(
                        f"wikilink: key {key!r} collision between "
                        f"{resolver._keys[key]} and {page.path}; last write wins",
                        stacklevel=2,
                    )
                resolver._keys[key] = page.path

        # Try to build the Aho-Corasick automaton. The import is deferred
        # to ``build()`` so tests can patch ``sys.modules`` cleanly and the
        # dep stays optional for users on platforms without a prebuilt wheel.
        try:
            import ahocorasick_rs  # type: ignore[import-not-found]

            keys_list = sorted(resolver._keys.keys())
            resolver._aho = ahocorasick_rs.AhoCorasick(keys_list)
            log.debug("wikilink: built Aho-Corasick automaton with %d keys", len(keys_list))
        except ImportError:
            log.info("wikilink: ahocorasick_rs not installed; using dict lookup")

        if not resolver._keys:
            log.warning("wikilink: corpus empty; all [[wikilinks]] will be flagged missing_page")

        return resolver

    def resolve(self, raw_target: str) -> Path | None:
        """Resolve a raw wikilink target to a corpus page path, or None."""
        if not raw_target:
            return None
        target = raw_target.strip()
        if target.lower().endswith((".md", ".markdown")):
            target = target[: target.rfind(".")]
        key = target.lower()
        if not key:
            return None
        aho = self._aho
        if aho is not None:
            # ahocorasick_rs reports every occurrence of a pattern as a
            # substring of the haystack; we only accept exact matches.
            # ``overlapping=True`` is required so that, when both ``foo`` and
            # ``foobar`` are keys, resolving ``foobar`` reports the longer
            # ``foobar`` match in addition to the leftmost ``foo`` match. The
            # default non-overlapping scan suppresses the longer match, which
            # would violate the spec's longest-wins guarantee.
            for match in aho.find_matches_as_strings(key, overlapping=True):
                if match == key:
                    return self._keys.get(key)
            return None
        return self._keys.get(key)
