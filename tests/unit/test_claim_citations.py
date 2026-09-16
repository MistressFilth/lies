from lies.orchestrator import _validate_claim_citations
from lies.query.citation import ClaimCitation


def test_validate_keeps_valid_entries() -> None:
    answer = "Each subagent runs in its own context. See b.md for details."
    citations = ["a.md", "b.md"]
    entries = [
        ClaimCitation(claim="Each subagent runs in its own context.", citation_index=0),
        ClaimCitation(claim="See b.md for details.", citation_index=1),
    ]
    kept, dropped = _validate_claim_citations(entries, citations, answer)
    assert len(kept) == 2
    assert dropped == []


def test_validate_drops_bad_index() -> None:
    answer = "Each subagent runs in its own context."
    citations = ["a.md"]
    entries = [ClaimCitation(claim="Each subagent runs in its own context.", citation_index=5)]
    kept, dropped = _validate_claim_citations(entries, citations, answer)
    assert kept == []
    assert len(dropped) == 1
    assert "out of range" in dropped[0]


def test_validate_drops_claim_not_in_answer() -> None:
    citations = ["a.md"]
    entries = [ClaimCitation(claim="not in body", citation_index=0)]
    kept, dropped = _validate_claim_citations(entries, citations, "actual body")
    assert kept == []
    assert len(dropped) == 1
    assert "not in body" in dropped[0]


def test_validate_drops_negative_index() -> None:
    citations = ["a.md"]
    entries = [ClaimCitation(claim="x", citation_index=-1)]
    kept, dropped = _validate_claim_citations(entries, citations, "x")
    assert kept == []


def test_validate_keeps_mixed() -> None:
    answer = "foo bar"
    citations = ["a.md", "b.md"]
    entries = [
        ClaimCitation(claim="foo", citation_index=0),  # valid
        ClaimCitation(claim="bar", citation_index=99),  # bad index
        ClaimCitation(claim="missing", citation_index=0),  # bad claim
    ]
    kept, dropped = _validate_claim_citations(entries, citations, answer)
    assert len(kept) == 1
    assert kept[0].claim == "foo"
    assert len(dropped) == 2


def test_validate_empty_input() -> None:
    kept, dropped = _validate_claim_citations([], [], "")
    assert kept == []
    assert dropped == []
