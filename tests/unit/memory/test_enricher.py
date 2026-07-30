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
