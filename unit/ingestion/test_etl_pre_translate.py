import pytest
from pydantic_ai.models.test import TestModel

from lies.etl.cost import CostBudget
from lies.etl.errors import BudgetExceeded
from lies.etl.query.pre_translate import pre_translate


def test_pre_translate_noop_when_no_operators() -> None:
    intent = pre_translate("hello world", model=TestModel(), budget=CostBudget())
    assert intent.collection_filter == []
    assert intent.tag_filter == []
    assert intent.exclude_terms == []
    assert intent.body == "hello world"


def test_pre_translate_spends_budget() -> None:
    budget = CostBudget(calls=0, tokens=10_000)
    with pytest.raises(BudgetExceeded):
        pre_translate("+python -javascript how do I foo?", model=TestModel(), budget=budget)
