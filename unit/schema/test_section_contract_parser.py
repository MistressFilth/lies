import pytest
from lies.schema.loader import SchemaSectionContractInvalid, parse_section_contract

VALID_BLOCK = """\
## Section contract
- **overview** — `## Scope`, `## Page types`, `## Conventions`
- **entity** — `## Overview`, `## Description`, `## References`
- **concept** — `## Definition`, `## Examples`, `## Related`
- **comparison** — `## Compared`, `## Differences`, `## When to use which`
- **source** — `## Source`, `## Summary`, `## Pages informed`
- **synthesis** — `## Thesis`, `## Evidence`, `## Open Questions`
"""


def test_parses_all_six_types():
    contract = parse_section_contract(VALID_BLOCK)
    expected = {
        "overview": ["## Scope", "## Page types", "## Conventions"],
        "entity": ["## Overview", "## Description", "## References"],
        "concept": ["## Definition", "## Examples", "## Related"],
        "comparison": ["## Compared", "## Differences", "## When to use which"],
        "source": ["## Source", "## Summary", "## Pages informed"],
        "synthesis": ["## Thesis", "## Evidence", "## Open Questions"],
    }
    for page_type, headings in expected.items():
        assert contract.for_type(page_type) == headings


def test_absent_block_returns_empty():
    """No ``## Section contract`` block → empty contract."""
    contract = parse_section_contract("# Other doc\n\nNo contract here.\n")
    assert contract.for_type("synthesis") == []
    assert contract.for_type("entity") == []


@pytest.mark.parametrize(
    "bad_doc",
    [
        "## Section contract\n\n- **widget** — `## Foo`\n",  # unknown type
        "## Section contract\n\n- **entity** — `## Overview`\n- **entity** — `## Notes`\n",  # duplicate
        "## Section contract\n\n- **entity** — Overview, Description\n",  # missing backticks
        "## Section contract\n\n- **entity** — `## `, `## Notes`\n",  # empty heading
    ],
    ids=["unknown_type", "duplicate_type", "missing_backticks", "empty_heading"],
)
def test_malformed_block_raises(bad_doc: str):
    """Each malformed ``## Section contract`` block raises SchemaSectionContractInvalid."""
    with pytest.raises(SchemaSectionContractInvalid):
        parse_section_contract(bad_doc)


def test_two_blocks_second_wins():
    """A second ``## Section contract`` block replaces the first; omitted types disappear."""
    doc = VALID_BLOCK + "\n## Section contract\n\n- **entity** — `## Just One`\n"
    contract = parse_section_contract(doc)
    assert contract.for_type("entity") == ["## Just One"]
    assert contract.for_type("synthesis") == []


def test_whitespace_and_newlines_tolerated():
    doc = "## Section contract\n\n  -  **entity**  —  `## Foo` , `## Bar`  \n"
    contract = parse_section_contract(doc)
    assert contract.for_type("entity") == ["## Foo", "## Bar"]
