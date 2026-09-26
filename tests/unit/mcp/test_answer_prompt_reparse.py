"""Defensive re-parse in the ``/answer`` prompt when the primary parse misses.

Live-debug bug (CC session ``db4bd25e-e72e-4eee-8c25-0645d37f82ef``
L8; OC sessions ``ses_f21324da7ffeQEEnIUm6VaJdg4`` /
``ses_f213135a4ffe5WB1GgnHT8Hu15`` /
``ses_f212dda62ffeZC3YUoGGQp725Q`` /
``ses_f2114ad31ffecMtKJfM41lBf1q`` /
``ses_f2110b5fdffeTHmy7K0dkIIQaj``): the ``/answer`` slash prompt
rendered the ``call-the-synthesize`` body with ``tag_expr: None`` and
the ``+c:opencode|c:claude_code`` prefix unstripped inside the
question field. The model obediently forwarded those kwargs to
``synthesize``, so the librarian ran untagged and qmd's hit ranking
surfaced the wrong-collection pages (CC L22/L44: 100%
``claude_code/*`` hits despite
``tag_expr='c:opencode|c:claude_code'``).

The OpenCode TUI's exact ``text`` arg could not be reverse-engineered
from the post-hoc session data, so the fix lives at the server side
as a defensive fallback in ``_defensive_reparse_filter``: when the
primary parse returns ``include_ast=None`` but the rendered
``parsed_question`` still starts with a filter sigil, re-parse the
question as if it were the full slash input.

These tests exercise the defense function directly with the bug case
(primary parse left the question unstripped) plus the regression
pins (primary parse succeeded → no re-parse; primary parse produced
a plain question → no re-parse).
"""

from __future__ import annotations

from lies.mcp.server import (
    _defensive_reparse_filter,
    _render_answer_prompt_body,
    ask_wiki_answer,
)
from lies.query.tag_expr import Include


def _render(text: str) -> str:
    """Render the ``/answer`` prompt body for the given ``text`` argument.

    ``ask_wiki_answer`` is the bare decorated function in this test
    process; FastMCP wraps it in a tool descriptor only when the
    server is mounted. Calling the bare function exercises the same
    body-rendering code path the prompt takes under live dispatch.
    """
    return ask_wiki_answer(text)


def _extract_fields(body: str) -> dict[str, str]:
    """Recover the rendered ``question`` / ``tag_expr`` / ``exclude_tags`` from a body."""
    fields: dict[str, str] = {}
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("question:"):
            fields["question"] = line[len("question:") :].strip()
        elif line.startswith("tag_expr:"):
            fields["tag_expr"] = line[len("tag_expr:") :].strip()
        elif line.startswith("exclude_tags:"):
            fields["exclude_tags"] = line[len("exclude_tags:") :].strip()
    return fields


# ---------------------------------------------------------------------------
# Direct unit tests of the defensive reparse helper
# ---------------------------------------------------------------------------


def test_defense_noop_when_include_ast_already_set() -> None:
    """Defense passes through when primary parse produced an include AST.

    Regression pin: the defense is a fallback, not a rewriter. When
    ``include_ast`` is already set, the defense returns the input
    triple unchanged regardless of question content.
    """
    include = Include("opencode", qualifier="c")
    q, inc, exc = _defensive_reparse_filter(
        "How do I configure X?",
        include,
        None,
    )
    assert q == "How do I configure X?"
    assert inc is include
    assert exc is None


def test_defense_noop_for_plain_question() -> None:
    """Defense does not trigger when question has no leading filter sigil.

    Regression pin: a legitimate question (no filter) must not be
    mutated by the defense. ``include_ast=None`` with a plain
    question is the canonical "no filter" path.
    """
    q, inc, exc = _defensive_reparse_filter(
        "How do I configure networking?",
        None,
        None,
    )
    assert q == "How do I configure networking?"
    assert inc is None
    assert exc is None


def test_defense_noop_for_question_with_mid_sentence_plus() -> None:
    """Mid-sentence ``+`` does NOT trigger the re-parse.

    Only a LEADING ``+`` token triggers the defense heuristic. A
    mid-sentence plus sign is just punctuation.
    """
    q, inc, exc = _defensive_reparse_filter(
        "How do I configure foo + bar?",
        None,
        None,
    )
    assert q == "How do I configure foo + bar?"
    assert inc is None
    assert exc is None


def test_defense_strips_single_include_atom() -> None:
    """``include_ast=None`` + question starts with ``+c:foo ...`` → re-parse strips it.

    Mirrors the bug pattern: primary parse returned no include AST
    but the question still carries the leading sigil. Re-parse
    extracts the include atom and the rest becomes the question.
    """
    q, inc, exc = _defensive_reparse_filter(
        "+c:opencode How do I configure X?",
        None,
        None,
    )
    assert q == "How do I configure X?"
    assert isinstance(inc, Include)
    assert inc.tag == "opencode"
    assert inc.qualifier == "c"
    assert exc is None


def test_defense_strips_or_include_chain() -> None:
    """``+c:opencode|c:claude_code How do X compare?`` → OR of two includes.

    Mirrors the user's first CC bug invocation
    (``tag_expr='c:opencode|c:claude_code'``). The defense must
    preserve the union semantics — both collections end up named in
    the include AST.
    """
    q, inc, exc = _defensive_reparse_filter(
        "+c:opencode|c:claude_code How do OpenCode and Claude Code compare?",
        None,
        None,
    )
    assert q == "How do OpenCode and Claude Code compare?"
    assert inc is not None
    # The OR node carries both Include atoms.
    from lies.query.tag_expr import Or

    assert isinstance(inc, Or)
    leaves = {inc.left, inc.right}
    assert Include("opencode", qualifier="c") in leaves
    assert Include("claude_code", qualifier="c") in leaves


def test_defense_strips_and_include_chain() -> None:
    """``+c:opencode&c:claude_code X?`` → AND of two includes."""
    from lies.query.tag_expr import And

    q, inc, exc = _defensive_reparse_filter(
        "+c:opencode&c:claude_code what about plugin authoring?",
        None,
        None,
    )
    assert q == "what about plugin authoring?"
    assert isinstance(inc, And)
    assert {inc.left, inc.right} == {
        Include("opencode", qualifier="c"),
        Include("claude_code", qualifier="c"),
    }


def test_defense_handles_t_qualifier() -> None:
    """``+t:linux ...`` → include AST with ``t:linux`` tag.

    The defense is qualifier-agnostic — ``t:`` atoms survive the
    same way ``c:`` atoms do. The downstream render surfaces them
    in ``tag_expr`` identically.
    """
    q, inc, exc = _defensive_reparse_filter(
        "+t:linux How do I configure networking?",
        None,
        None,
    )
    assert q == "How do I configure networking?"
    assert isinstance(inc, Include)
    assert inc.tag == "linux"
    assert inc.qualifier == "t"


def test_defense_noop_when_reparse_finds_no_atom() -> None:
    """Re-parse yielding no include AST → original triple returned.

    When the leading-sigil heuristic fires but the re-parse returns
    ``include_ast=None`` (e.g. ``+c:`` with an empty body — the
    parser raises ``TagExprParseError("qualifier 'c' without
    atom")`` which we catch), the original triple stands.
    """
    q, inc, exc = _defensive_reparse_filter(
        "+c: How do I configure X?",
        None,
        None,
    )
    # Re-parse raised; original (empty include_ast) is preserved.
    assert q == "+c: How do I configure X?"
    assert inc is None
    assert exc is None


# ---------------------------------------------------------------------------
# Integration tests — confirm the live prompt body honors the defense
# ---------------------------------------------------------------------------


def test_live_render_strips_filter_when_primary_parse_missed() -> None:
    """End-to-end: prompt body shows stripped question + tag_expr when defense fires.

    Drives the defense via the live prompt body for a ``text`` that
    exercises the common bug shape (primary parse missed the
    sigil because the OpenCode TUI slash picker threaded the
    slash-input through some path that left the leading ``+``
    attached to a non-leading argv token). We can't pin down the
    exact upstream shape from post-hoc session data — the defense
    catches the documented "leading sigil embedded in question"
    pattern, which the OC session transcripts all satisfy.

    Constructed by mimicking the parsed-question state the bug
    produces (primary parse returned ``include_ast=None`` and the
    question is the unstripped input). Driving the live
    ``ask_wiki_answer`` directly with the input would already work
    correctly because the primary parse handles the leading-``+``
    case — so we exercise the defense in isolation here.
    """
    # The defense's job: when primary parse returned no include AST
    # AND the question still carries a leading filter sigil, re-parse.
    q, inc, exc = _defensive_reparse_filter(
        "+c:opencode|c:claude_code How do OpenCode and Claude Code compare?",
        None,
        None,
    )
    assert q == "How do OpenCode and Claude Code compare?"
    assert inc is not None

    # Now drive the live prompt: the primary parse already gets the
    # right answer for the well-formed case (the question the live
    # prompt sees already has the filter stripped). The defense
    # triggers only on the bug shape; here it should be a no-op.
    body = _render("+c:opencode|c:claude_code How do OpenCode and Claude Code compare?")
    f = _extract_fields(body)
    assert f["question"] == "How do OpenCode and Claude Code compare?"
    assert f["tag_expr"] == "'c:opencode|c:claude_code'"


def test_live_render_full_prompt_body_for_legitimate_question() -> None:
    """A plain ``/answer`` invocation renders the canonical body unchanged.

    Regression pin: the defense is fail-soft. Plain questions
    without filter sigils render the same body they did before
    the defense was added.
    """
    body = _render("How do I configure networking?")
    f = _extract_fields(body)
    assert f["question"] == "How do I configure networking?"
    assert f["tag_expr"] == "None"
    assert f["exclude_tags"] == "[]"


def test_live_render_full_prompt_body_for_filter_question() -> None:
    """A ``/answer +c:opencode ...`` invocation renders the body with tag_expr set.

    Regression pin: the primary parse handles the well-formed case
    (``include_ast`` set on the first try) — the defense is a no-op
    here, but the end-to-end render still threads through cleanly.
    """
    body = _render("+c:opencode How do I configure X?")
    f = _extract_fields(body)
    assert f["question"] == "How do I configure X?"
    assert f["tag_expr"] == "'c:opencode'"
    assert f["exclude_tags"] == "[]"


def test_render_answer_prompt_body_format_includes_field_labels() -> None:
    """The body uses canonical ``question:`` / ``tag_expr:`` / ``exclude_tags:`` labels."""
    body = _render_answer_prompt_body(question="q", tag_expr=None, exclude_tags=[])
    assert "question:" in body
    assert "tag_expr:" in body
    assert "exclude_tags:" in body
    assert "Do NOT modify" in body
    assert "verbatim" in body
