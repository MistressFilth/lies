"""`AGENTS.md` makes checkable claims about the seam. Check them.

`AGENTS.md` is the repository's declared source of truth for agents
working here, so a false sentence in it is not a style problem — it
is a wrong answer delivered to whoever reads it next. Several of its
paragraphs about the qmd seam assert specific facts about the code:
which tools the daemon serves, that a wedge carries its log tail on
*both* paths, that the batched read classifies transport errors.

Those facts were false at least once. The seam section claimed
``ground`` "filters silently and logs" on a code path that logged
nothing, and the read section's claim that the batched read raises on
a wedged daemon described a function whose `except _DAEMON_FAILURES`
was unreachable. Both sentences survived review because nothing
compared them to the code.

A doc that asserts a fact about code gets a test, the same way the
taxonomy's probe results got one.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path


from lies.qmd import access
from lies.qmd import _subprocess

AGENTS_MD = Path("AGENTS.md")


def _agents_text() -> str:
    return AGENTS_MD.read_text(encoding="utf-8")


def _called_names(source: str) -> set[str]:
    """Every function called in ``source`` — attribute or bare name.

    Both forms: the batched read calls ``_call_with_recovery`` as a
    module-local bare name, and an attribute-only scan reads that as
    a call the function does not make.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Attribute):
            names.add(node.func.attr)
        elif isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


def test_the_documented_capability_map_matches_the_module() -> None:
    """The daemon/CLI split in `AGENTS.md` is the split in the code."""
    text = _agents_text()
    assert "DAEMON_TOOLS" in text, "AGENTS.md no longer names the capability map at all"
    for tool in sorted(access.DAEMON_TOOLS):
        assert f"`{tool}`" in text, f"AGENTS.md omits a daemon tool the code serves: {tool}"
    for op in sorted(access.CLI_ONLY_OPS):
        assert f"`{op}`" in text, f"AGENTS.md omits a CLI-only op the code declares: {op}"


def test_the_documented_taxonomy_gap_is_still_a_gap() -> None:
    """`AGENTS.md` records that three httpx classes skip the recycle.

    A recorded *decision* rots quietly: the reason a class is
    excluded is not re-derived on the next edit, so the exclusion
    outlives its justification. This pins the shape of the gap
    rather than the prose.
    """
    assert access._is_local_protocol_error(type("LocalProtocolError", (Exception,), {})()), (
        "the local-protocol class is no longer recognised by name"
    )
    for name in ("LocalProtocolError", "HTTPStatusError", "RemoteProtocolError"):
        assert name in _agents_text(), (
            f"AGENTS.md records a deliberate taxonomy gap for {name}; the "
            f"decision is documented as considered and must not disappear"
        )


def test_both_wedge_paths_carry_the_log_tail() -> None:
    """Every ``QmdWedgeError`` raise site attaches ``stderr``.

    The two-site attribution is load-bearing: a wedge reported with
    no tail tells the reader only what they already knew, and
    `AGENTS.md` says the tail is the evidence. Both the idle-bound and
    the total-bound raise must carry it, or the recorded rationale no
    longer matches the code.
    """
    source = inspect.getsource(_subprocess)
    tree = ast.parse(source)
    raises = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and getattr(node.exc.func, "id", "") == "QmdWedgeError"
    ]
    assert raises, "no QmdWedgeError raise sites found in _subprocess"
    for node in raises:
        keys = {kw.arg for kw in node.exc.keywords}
        assert "last_output" in keys, (
            f"a QmdWedgeError at line {node.lineno} carries no last_output; "
            f"AGENTS.md records the tail as the evidence a wedge needs"
        )
        assert "stderr" in keys, f"a QmdWedgeError at line {node.lineno} carries no stderr"


def test_the_read_section_claims_what_the_code_does() -> None:
    """`AGENTS.md` does not promise a taxonomy the batched read skips.

    The claim was false once: the section said a wedged daemon
    re-raised, while `read_library_bodies` swallowed every transport
    failure into a per-path `None`. Rather than assert the prose
    matches, this asserts the code matches what the prose *should*
    say — the batched read routes through the classifier.
    """
    from lies.mcp import read as read_mod

    calls = _called_names(inspect.getsource(read_mod))
    assert "read_library_bodies" in calls, (
        "read.py no longer calls the batched seam; AGENTS.md documents the "
        "batched read as the library-path transport"
    )

    # The seam's *own* body, not the module's. A module-level string
    # check passes as long as ``_call_with_recovery`` appears anywhere
    # in the file -- including on the ``daemon_tool`` path, which
    # classified all along -- so it would have passed against exactly
    # the defect this names.
    fanout = _called_names(inspect.getsource(access.read_library_bodies))
    assert "_call_with_recovery" in fanout, (
        "read_library_bodies no longer routes through the shared recovery; a "
        "transport failure becomes a per-path None, which read.py reports as "
        "a document the daemon could not resolve"
    )


def test_the_ground_section_agrees_with_the_grounding_module() -> None:
    """The `ground` scope claim matches what the module does.

    `AGENTS.md` said `ground` "filters silently and logs" on a path
    that bound the unknown set to `_unknown` and logged nothing. Both
    halves were false; the fix is a log call and a digest field, and
    this pins that neither can be removed silently.
    """
    from lies.mcp import grounding

    assert hasattr(grounding.ArchivistDigest, "__dataclass_fields__")
    assert "unserved_scope" in grounding.ArchivistDigest.__dataclass_fields__, (
        "ArchivistDigest lost unserved_scope; AGENTS.md documents the "
        "searched / unserved split as the fix for a scope problem reading "
        "as an empty corpus"
    )
    source = inspect.getsource(grounding)
    assert "does not serve" in source, (
        "grounding.py no longer logs the unserved collection names, which is "
        "half of what AGENTS.md now claims it does"
    )


def test_the_known_flakes_section_names_the_measured_failures() -> None:
    """`## Known flakes` is a contract, not a scratchpad.

    The CUDA reservation flake lived only in a revert commit message,
    so the next agent tried the same retry. Its home is a section
    with a heading, and this fails if the heading is renamed or the
    measurement table is deleted along with the commit.
    """
    text = _agents_text()
    assert "## Known flakes" in text, "AGENTS.md lost its Known flakes section"
    assert "cuMemAddressReserve" in text, (
        "the CUDA reservation flake lost its AGENTS.md entry; the next agent "
        "will re-try a mitigation that was measured and rejected"
    )
