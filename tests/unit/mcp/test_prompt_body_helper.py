"""Pin ``rendered_body`` itself.

The helper fell through to ``str(message)`` because a FastMCP
``Message`` carries its text on ``content.text``, not on a top-level
``.text``. Every ``str(Message)`` returns pydantic's repr, so twelve
assertions across the prompt tests were matching ``\\"`` and ``\\n``
escape sequences rather than the body the agent receives. The tests
passed, which is why the defect survived.

A test on the helper is what makes the next occurrence loud: every
prompt assertion is downstream of this function, so a silent fallthrough
here invalidates the whole file.
"""

from __future__ import annotations

from fastmcp.prompts import Message

from tests.unit.mcp._prompt_body import carries_verbatim, rendered_body


def test_reads_the_text_a_fastmcp_message_actually_carries() -> None:
    body = rendered_body(Message("it's\nfine"))
    assert body == "it's\nfine"
    assert "\\n" not in body
    assert "TextContent" not in body


def test_does_not_fall_through_to_str() -> None:
    """The regression, stated as the thing that must not happen."""
    [msg] = (Message("plain body text"),)
    assert rendered_body(msg) == "plain body text"
    assert rendered_body(msg) != str(msg)


def test_keeps_a_body_that_looks_like_a_pydantic_repr() -> None:
    """A body which is itself repr-shaped still comes back verbatim."""
    [msg] = (Message("run 'lies sync'"),)
    assert rendered_body(msg) == "run 'lies sync'"


def test_carries_verbatim_rejects_a_repr_escaped_block() -> None:
    """``carries_verbatim`` is the assertion that must fail on a repr.

    A question carrying a newline renders into a fenced block; the old
    helper handed the assertion a repr, and the repr's ``\\n`` matched
    nothing — the assertion went vacuous rather than red. Pin the
    rejection explicitly so a future helper cannot pass it by
    accident.
    """
    assert carries_verbatim("body\n```\nwhat is x\n```", "what is x")
    assert not carries_verbatim("body\n```\nwhat is x\\nmore\n```", "what is x\nmore")
    assert not carries_verbatim("body with no fence", "what is x")
