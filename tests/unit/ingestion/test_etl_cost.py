import pytest

from lies.etl.cost import CostBudget
from lies.etl.errors import BudgetExceeded


def test_budget_default_caps() -> None:
    b = CostBudget()
    assert b.remaining == (10, 500_000)


def test_budget_spend_calls_and_tokens() -> None:
    b = CostBudget(calls=5, tokens=1000)
    b.spend(calls=2, tokens=200)
    assert b.remaining == (3, 800)


def test_budget_exceeded_calls() -> None:
    b = CostBudget(calls=1, tokens=10_000)
    with pytest.raises(BudgetExceeded) as ei:
        b.spend(calls=2)
    assert ei.value.spent == (2, 0)
    assert ei.value.cap == (1, 10_000)


def test_budget_exceeded_tokens() -> None:
    b = CostBudget(calls=10, tokens=100)
    with pytest.raises(BudgetExceeded):
        b.spend(tokens=200)


def test_negative_cap_rejected() -> None:
    with pytest.raises(ValueError):
        CostBudget(calls=-1)
    with pytest.raises(ValueError):
        CostBudget(tokens=-1)
    with pytest.raises(ValueError):
        CostBudget(calls=-1, tokens=-1)


def test_negative_spend_rejected() -> None:
    b = CostBudget(calls=5, tokens=1000)
    with pytest.raises(ValueError):
        b.spend(calls=-1)
    with pytest.raises(ValueError):
        b.spend(tokens=-1)
    with pytest.raises(ValueError):
        b.spend(calls=-1, tokens=-1)
