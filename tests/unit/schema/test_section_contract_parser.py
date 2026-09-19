import pytest
from lies.schema.loader import (
    SchemaSectionContractInvalid,
    parse_section_contract,
)


VALID_BLOCK = """\
## Section contract

Each page type must carry the following `## <Heading>` lines.

- **overview** — `## Scope`, `## Page types`, `## Conventions`
- **entity** — `## Overview`, `## Description`, `## References`
- **concept** — `## Definition`, `## Examples`, `## Related`
- **comparison** — `## Compared`, `## Differences`, `## When to use which`
- **source** — `## Source`, `## Summary`, `## Pages informed`
- **synthesis** — `## Thesis`, `## Evidence`, `## Open Questions`
"""


def test_parses_all_six_types():
    contract = parse_section_contract(VALID_BLOCK)
    assert contract.for_type("overview") == [
        "## Scope",
        "## Page types",
        "## Conventions",
    ]
    assert contract.for_type("entity") == [
        "## Overview",
        "## Description",
        "## References",
    ]
    assert contract.for_type("concept") == [
        "## Definition",
        "## Examples",
        "## Related",
    ]
    assert contract.for_type("comparison") == [
        "## Compared",
        "## Differences",
        "## When to use which",
    ]
    assert contract.for_type("source") == [
        "## Source",
        "## Summary",
        "## Pages informed",
    ]
    assert contract.for_type("synthesis") == [
        "## Thesis",
        "## Evidence",
        "## Open Questions",
    ]


def test_absent_block_returns_empty():
    contract = parse_section_contract("# Other doc\n\nNo contract here.\n")
    assert contract.for_type("synthesis") == []
    assert contract.for_type("entity") == []


def test_unknown_type_raises():
    bad = "## Section contract\n\n- **widget** — `## Foo`\n"
    with pytest.raises(SchemaSectionContractInvalid):
        parse_section_contract(bad)


def test_duplicate_type_raises():
    bad = "## Section contract\n\n- **entity** — `## Overview`\n- **entity** — `## Notes`\n"
    with pytest.raises(SchemaSectionContractInvalid):
        parse_section_contract(bad)


def test_missing_backticks_raises():
    bad = "## Section contract\n\n- **entity** — Overview, Description\n"
    with pytest.raises(SchemaSectionContractInvalid):
        parse_section_contract(bad)


def test_empty_heading_inside_backticks_raises():
    bad = "## Section contract\n\n- **entity** — `## `, `## Notes`\n"
    with pytest.raises(SchemaSectionContractInvalid):
        parse_section_contract(bad)


def test_two_blocks_second_wins():
    doc = VALID_BLOCK + "\n## Section contract\n\n- **entity** — `## Just One`\n"
    contract = parse_section_contract(doc)
    assert contract.for_type("entity") == ["## Just One"]
    # synthesis disappears (second block omits it)
    assert contract.for_type("synthesis") == []


def test_whitespace_and_newlines_tolerated():
    doc = "## Section contract\n\n  -  **entity**  —  `## Foo` , `## Bar`  \n"
    contract = parse_section_contract(doc)
    assert contract.for_type("entity") == ["## Foo", "## Bar"]


def test_zero_headings_allowed():
    doc = "## Section contract\n\n- **entity** — (none)\n"
    contract = parse_section_contract(doc)
    assert contract.for_type("entity") == []
