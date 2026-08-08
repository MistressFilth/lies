"""WikiLink extraction and resolution.

Distinct from QMD (semantic search). Uses an Aho-Corasick automaton over
a corpus of wiki-page identifiers (filename stem, frontmatter title,
frontmatter aliases) to resolve ``[[WikiLink]]`` targets and detect
broken links.
"""

from __future__ import annotations

import re

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


def _strip_code_spans(text: str) -> str:
    """Strip fenced + inline code spans so wikilinks inside them aren't matched."""
    text = _FENCED_CODE_RE.sub("", text)
    text = _INLINE_CODE_RE.sub("", text)
    return text


def extract_wikilinks(text: str) -> list[str]:
    """Return the raw ``[[target]]`` strings in ``text``, code spans excluded."""
    cleaned = _strip_code_spans(text)
    return [m.group("target") for m in _WIKILINK_RE.finditer(cleaned)]
