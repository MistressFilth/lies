"""Models for the query layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from lies.memory.models import MemoryReceipt
from lies.query.citation import Citation, ClaimCitation


@dataclass(frozen=True)
class SynthesizedAnswer:
    """The result of synthesizing an answer from wiki pages. See "Data shapes" in
    AGENTS.md for the field contract.
    """

    answer: str
    citations: list[Citation] = field(default_factory=list)
    pages_read: list[Citation] = field(default_factory=list)
    claim_citations: tuple[ClaimCitation, ...] = ()
    fallback_used: bool = False
    fallback_reason: str = ""
    changed_pages: list[str] = field(default_factory=list)
    page_links: list[str] = field(default_factory=list)
    synthesis_used: bool = False
    synthesis_reason: str = ""
    should_file: bool = False
    question: str = ""
    file_receipt: MemoryReceipt | None = None
    searched_scope: list[str] = field(default_factory=list)
    format: Literal["md", "table", "marp", "chart"] = "md"

    def __post_init__(self) -> None:
        """Coerce ``claim_citations`` into the declared tuple shape.

        ``dataclass`` does not perform runtime type coercion, so a
        list passed to a ``tuple`` field stays a list. We freeze the
        value to match the annotation so downstream equality checks
        like ``ans.claim_citations == (cc,)`` hold regardless of
        whether the caller passed a list or a tuple.
        """
        value = self.claim_citations
        if not isinstance(value, tuple):
            value = tuple(value)
        object.__setattr__(self, "claim_citations", value)
