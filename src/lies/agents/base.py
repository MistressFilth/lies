"""Shared helpers for sub-agents."""

from __future__ import annotations

from typing import Any, TypeVar

from pydantic import BaseModel
from pydantic_ai import Agent

SUB_AGENT_SYSTEM_PROMPT_PREFIX = """You are a LIES wiki sub-agent. You operate
inside a Karpathy-pattern LLM wiki. The user is curating a knowledge base over
a corpus of sources. Your job is to do one specific task precisely and return a
structured result.

The wiki is a git repository of markdown files. The schema that defines the
wiki structure is:

"""


T = TypeVar("T", bound=BaseModel)


def make_sub_agent(
    model: str,
    output_type: type[T],
    system_prompt: str,
    tools: list[Any] | None = None,
) -> Agent[None, T]:
    """Construct a pydantic-ai sub-agent with the LIES system prompt prefix."""
    return Agent(
        model,
        output_type=output_type,
        system_prompt=SUB_AGENT_SYSTEM_PROMPT_PREFIX + system_prompt,
        tools=tools or [],
    )
