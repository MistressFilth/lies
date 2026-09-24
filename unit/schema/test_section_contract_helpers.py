from lies.schema.sections import (
    SectionContract,
    _missing_required_sections,
)


def make_contract() -> SectionContract:
    return SectionContract(
        entity=["## Overview", "## Description", "## References"],
        synthesis=["## Thesis", "## Evidence", "## Open Questions"],
    )


def test_missing_returns_absent_in_order():
    contract = make_contract()
    body = "## Overview\n\nSome text.\n"
    assert _missing_required_sections("entity", contract, body) == [
        "## Description",
        "## References",
    ]


def test_no_missing_when_all_present():
    contract = make_contract()
    body = "## Overview\n\nx\n\n## Description\n\nx\n\n## References\n\nx\n"
    assert _missing_required_sections("entity", contract, body) == []


def test_unknown_type_returns_empty():
    contract = make_contract()
    assert _missing_required_sections("widget", contract, "## Overview\n") == []


def test_literal_substring_no_setext():
    """## Evidence\n=====\n is NOT a match — literal substring only."""
    contract = SectionContract(synthesis=["## Evidence"])
    body = "Evidence\n=====\n\nbody\n"
    assert _missing_required_sections("synthesis", contract, body) == ["## Evidence"]


def test_case_sensitive():
    contract = SectionContract(synthesis=["## Evidence"])
    body = "## evidence\n\nx\n"
    assert _missing_required_sections("synthesis", contract, body) == ["## Evidence"]
