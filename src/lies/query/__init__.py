"""Query layer: index parsing and synthesizer with qmd fallback."""

from lies.query.index_parser import IndexLink, parse_index_links
from lies.query.models import SynthesizedAnswer
from lies.query.synthesizer import (
    DEFAULT_TOP_N,
    FALLBACK_REASON_FAILED,
    FALLBACK_REASON_NO_RESULTS,
    FALLBACK_REASON_UNAVAILABLE,
    PageRead,
    build_answer_from_pages,
    retrieve_pages,
    set_qmd_search,
    synthesize_answer,
)

__all__ = [
    "DEFAULT_TOP_N",
    "FALLBACK_REASON_FAILED",
    "FALLBACK_REASON_NO_RESULTS",
    "FALLBACK_REASON_UNAVAILABLE",
    "IndexLink",
    "PageRead",
    "SynthesizedAnswer",
    "build_answer_from_pages",
    "parse_index_links",
    "retrieve_pages",
    "set_qmd_search",
    "synthesize_answer",
]
