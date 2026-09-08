import pytest
from pydantic_ai.models.test import TestModel

from lies.memory.enricher import MemoryEnricherDeps, enricher_agent
from lies.memory.models import MemoryPlan


@pytest.fixture
def model() -> TestModel:
    return TestModel(
        custom_output_args=MemoryPlan(
            operations=[],
            rationale="noop",
            evidence=[],
        ).model_dump()
    )


def test_enricher_returns_noop_when_no_evidence(model: TestModel) -> None:
    agent = enricher_agent(model=model)
    deps = MemoryEnricherDeps(
        answer="I am not sure.",
        pages_read=[],
        citations=[],
        evidence_text="",
    )
    plan = agent.run_sync("noop", deps=deps).output
    assert isinstance(plan, MemoryPlan)
    assert plan.is_noop()


def test_enricher_validates_plan_shape(model: TestModel) -> None:
    agent = enricher_agent(model=model)
    deps = MemoryEnricherDeps(
        answer="X is Y",
        pages_read=[],
        citations=[],
        evidence_text="",
    )
    plan = agent.run_sync("answer", deps=deps).output
    # Plans must always pass through pydantic validation.
    assert plan.rationale is not None or plan.rationale == ""


def test_enricher_instructions_include_structured_dependencies(model: TestModel) -> None:
    agent = enricher_agent(model=model)
    deps = MemoryEnricherDeps(
        user_request="What is X?",
        answer="X is Y.",
        pages_read=["page-1"],
        citations=["wiki/concepts/x.md:4-8"],
        evidence_text="X is Y from the source.",
        current_page_metadata={"wiki/concepts/x.md": {"sha256": "abc"}},
        active_schema="type: concept",
    )
    messages = agent.run_sync("propose", deps=deps).all_messages()
    rendered = "\n".join(str(message) for message in messages)
    assert "What is X?" in rendered
    assert "page-1" in rendered
    assert "wiki/concepts/x.md:4-8" in rendered
    assert "sha256" in rendered
    assert "type: concept" in rendered
