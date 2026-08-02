"""CollectionAuthorAgent sub-agent.

Drives a one-question-at-a-time conversation that produces a
``Collection`` proposal from a manifest of available source files.
The CLI renders questions via ``rich.prompt`` and feeds answers back
via ``message_history``.

Output type is a tagged union: ``AuthorQuestion`` requests the next
answer; ``AuthorProposal`` returns the final ``Collection`` (as a
serialized dict, since the dataclass itself is not a pydantic model)
plus a rationale.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, Field
from pydantic_ai import Agent

AUTHOR_SYSTEM_PROMPT = """\
You are the LIES collection author. The user wants to ingest a
documentation corpus into a LIES wiki. You receive a manifest of
files available at the source URL plus a short user prompt.

Your job: ask one question at a time, then emit a final proposal
containing a complete `Collection` record. The CLI will render each
question via `rich.prompt` and feed the answer back to you.

Always include:
- `name` (filesystem-safe; no `+ & | -` characters)
- `source` (the original URL or path the user gave you)
- `source_format` (one of `markdown`, `html`, `rst`, `pdf`, `sphinx`,
  `bespoke`, `liquid` — choose based on the manifest contents)
- `config` (a free-form map; for `sphinx` you may populate
  `sphinx_includes`, `sphinx_excludes`, `sphinx_renames`)

When you have enough information, return an AuthorProposal.
"""


class AuthorQuestion(BaseModel):
    """A single question the agent needs answered before it can propose."""

    id: str
    prompt: str
    options: list[str] | None = None
    default: str | None = None


class AuthorProposal(BaseModel):
    """The final proposal — a serialized Collection record plus rationale."""

    collection: dict[str, Any] = Field(
        description=("Serialized Collection record. Will be loaded via Collection(**payload).")
    )
    rationale: str


# Tagged union. We use a plain `|` (not Annotated[..., Field(discriminator=...)]):
# the brief's discriminated variant was verbatim-broken (neither variant
# declared a `kind` field), and the simpler form is sufficient for pydantic-ai
# to validate the agent's output against either shape.
AuthorOutput = AuthorQuestion | AuthorProposal


class CollectionAuthorDeps(BaseModel):
    """Per-run dependencies for the CollectionAuthorAgent.

    Carries the manifest of source files available at the source URL,
    so the agent can ask format-specific questions grounded in the
    actual contents of the corpus.
    """

    model_config = {"arbitrary_types_allowed": True}
    manifest: list[dict[str, Any]]


def collection_author_agent(
    model: Any | None = None,
) -> Agent[CollectionAuthorDeps, AuthorOutput]:
    """Construct the structured-output CollectionAuthorAgent."""
    resolved: Any = model if model is not None else "anthropic:claude-opus-4-7"
    # pydantic-ai's Agent constructor overloads don't include `type[X | Y]`
    # for `output_type`; the union is valid at runtime, so we cast to Any
    # to satisfy mypy while preserving the static return-type annotation.
    return Agent(
        resolved,
        deps_type=CollectionAuthorDeps,
        output_type=cast(Any, AuthorOutput),
        system_prompt=AUTHOR_SYSTEM_PROMPT,
    )
