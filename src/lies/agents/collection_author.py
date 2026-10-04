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

from dataclasses import dataclass
from typing import Any

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


@dataclass
class AuthorQuestion:
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


@dataclass
class CollectionAuthorDeps:
    """Per-run dependencies for the CollectionAuthorAgent.

    Carries the manifest of source files available at the source URL,
    so the agent can ask format-specific questions grounded in the
    actual contents of the corpus.
    """

    manifest: list[dict[str, Any]]


def collection_author_agent(
    model: Any | None = None,
) -> Agent[CollectionAuthorDeps, AuthorQuestion | AuthorProposal]:
    """Construct the structured-output CollectionAuthorAgent.

    The return annotation spells the union out rather than naming
    ``AuthorOutput``. They are the same type, but the alias does not
    survive as the agent's own type parameter, so a checker comparing
    the two does not see them as equal. Spelling it out is what the
    value is.
    """
    if model is None:
        from lies.errors import ModelNotConfigured

        raise ModelNotConfigured(
            "collection_author_agent requires an explicit model. "
            "Pass `model=` or set LIES_AGENT_COLLECTION_AUTHOR_MODEL "
            "/ configure providers.toml."
        )
    resolved: Any = model
    # The union goes to `output_type` uncast. It used to be
    # `cast(Any, AuthorOutput)`, on the claim that the Agent
    # constructor's overloads do not accept `type[X | Y]`; as of
    # pydantic-ai 2.54 they do. The cast was not buying silence so much
    # as erasing the type: the checker then inferred the constructor's
    # default `output_type` of `str`, so the declared return type was a
    # claim nothing in the body supported.
    return Agent(
        resolved,
        deps_type=CollectionAuthorDeps,
        output_type=AuthorOutput,
        system_prompt=AUTHOR_SYSTEM_PROMPT,
    )
