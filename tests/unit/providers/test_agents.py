"""AGENT_ROSTER is the single source of truth for agent names."""

from lies.providers.agents import AGENT_ROSTER


def test_roster_contains_all_known_agents() -> None:
    assert AGENT_ROSTER == (
        "orchestrator",
        "source_reader",
        "page_writer",
        "linter",
        "query_synthesizer",
        "enricher",
        "repair",
    )


def test_roster_names_are_unique() -> None:
    assert len(AGENT_ROSTER) == len(set(AGENT_ROSTER))


def test_roster_names_are_valid_identifiers() -> None:
    for name in AGENT_ROSTER:
        assert name.isidentifier(), f"{name!r} is not a valid Python identifier"
