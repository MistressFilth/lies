"""Per-type required-section contract.

The contract is declared in markdown (see ``default_schema.md`` →
``## Section contract``) and parsed into a frozen Pydantic model by
:func:`lies.schema.loader.parse_section_contract`. This module
holds the model and the literal-substring helper used by both
the writer (refusal) and the lint shell (finding).
"""

from pydantic import BaseModel, ConfigDict


class SectionContract(BaseModel):
    """Required `## <Heading>` strings per page type.

    Each field is the list of headings that page type must carry.
    Mirrors ask's ``RequiredSections`` shape with six fields
    (lies ships six page types; ask ships four).
    """

    model_config = ConfigDict(frozen=True)

    overview: list[str] = []
    entity: list[str] = []
    concept: list[str] = []
    comparison: list[str] = []
    source: list[str] = []
    synthesis: list[str] = []

    def for_type(self, page_type: str) -> list[str]:
        """Return required ## headings for ``page_type``, or ``[]``."""
        value: list[str] | None = getattr(self, page_type, None)
        return list(value) if isinstance(value, list) else []


def _missing_required_sections(
    page_type: str,
    contract: SectionContract,
    body: str,
) -> list[str]:
    """Return required ## headings absent from ``body``, in contract order.

    Literal-substring match — mirrors ask's behavior. Misses setext
    headings, case differences, and missing space after ``##``.
    Operators who write ``### Evidence`` get flagged; that's the
    signal to fix the heading form. Documented in
    ``default_schema.md`` under "Section contract".
    """
    return [h for h in contract.for_type(page_type) if h not in body]
