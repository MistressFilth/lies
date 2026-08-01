"""Per-sync model-call and token budget enforcement."""
from __future__ import annotations

from lies.etl.errors import BudgetExceeded


class CostBudget:
    def __init__(self, calls: int = 10, tokens: int = 500_000) -> None:
        self._cap_calls = calls
        self._cap_tokens = tokens
        self._calls = 0
        self._tokens = 0

    def spend(self, calls: int = 1, tokens: int = 0) -> None:
        self._calls += calls
        self._tokens += tokens
        if self._calls > self._cap_calls or self._tokens > self._cap_tokens:
            raise BudgetExceeded(
                spent=(self._calls, self._tokens),
                cap=(self._cap_calls, self._cap_tokens),
            )

    @property
    def remaining(self) -> tuple[int, int]:
        return (
            max(self._cap_calls - self._calls, 0),
            max(self._cap_tokens - self._tokens, 0),
        )
