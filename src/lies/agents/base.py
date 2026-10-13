"""Shared helpers for sub-agents."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models import Model

SUB_AGENT_SYSTEM_PROMPT_PREFIX = """You are a LIES wiki sub-agent. You operate
inside a Karpathy-pattern LLM wiki. The user is curating a knowledge base over
a corpus of sources. Your job is to do one specific task precisely and return a
structured result.

The wiki is a git repository of markdown files. The schema that defines the
wiki structure is:

"""


def make_sub_agent[T: BaseModel, D](
    model: Model | str,
    output_type: Any,
    system_prompt: str,
    tools: list[Any] | None = None,
    deps_type: type[D] | None = None,
    output_retries: int | None = None,
) -> Agent[Any, T]:
    """Construct a pydantic-ai sub-agent with the LIES system prompt prefix."""
    kwargs: dict[str, Any] = {}
    if output_retries is not None:
        kwargs["retries"] = output_retries
    return Agent(  # type: ignore[call-overload]
        model,
        output_type=output_type,
        system_prompt=SUB_AGENT_SYSTEM_PROMPT_PREFIX + system_prompt,
        tools=tools or [],
        deps_type=deps_type if deps_type is not None else object,
        **kwargs,
    )
