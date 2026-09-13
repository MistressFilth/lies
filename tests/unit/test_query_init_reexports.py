"""Re-export contract for ``lies.query``.

Pins the public-API surface of ``lies.query`` so callers can import
the canonical symbols (e.g. ``FALLBACK_REASON_WIKI_ONLY``, ``Citation``)
from the top-level package instead of drilling into a sub-module.
Catches regressions where a re-export is removed in a refactor.
"""

from __future__ import annotations

import lies.query
import lies.query.citation as _citation_module
import lies.query.synthesizer as _synthesizer_module


def test_fallback_reason_wiki_only_is_reexported() -> None:
    """``FALLBACK_REASON_WIKI_ONLY`` is the wiki-only fallback signal;
    callers should be able to import it from ``lies.query`` directly."""
    assert "FALLBACK_REASON_WIKI_ONLY" in lies.query.__all__
    assert lies.query.FALLBACK_REASON_WIKI_ONLY is _synthesizer_module.FALLBACK_REASON_WIKI_ONLY


def test_citation_is_reexported() -> None:
    """``Citation`` is the citation model; callers should be able to
    import it from ``lies.query`` directly without reaching into
    ``lies.query.citation``."""
    assert "Citation" in lies.query.__all__
    assert lies.query.Citation is _citation_module.Citation


def test_existing_fallback_reason_reexports_still_present() -> None:
    """Regression guard: previously re-exported fallback constants
    must stay re-exported so the existing contract does not
    silently narrow."""
    for name in (
        "FALLBACK_REASON_FAILED",
        "FALLBACK_REASON_NO_RESULTS",
        "FALLBACK_REASON_UNAVAILABLE",
    ):
        assert name in lies.query.__all__
        assert getattr(lies.query, name) is getattr(_synthesizer_module, name)
