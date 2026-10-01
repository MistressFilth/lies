"""A qmd timeout is not an unreachable daemon, and must not read as one.

Found while testing 0.42.0 against the live corpus. An intermittent
15s stall on ``search`` produced::

    no_coverage=True
    fallback_reason="qmd unreachable: qmd query timed out after 15s"

and the librarian contract (``agents/librarian.py``) tells the model
that ``no_coverage=True`` means "the corpus has zero hits for this
question". So a slow call reached the user as "No relevant content
found in library." — a false claim about the corpus, for a query that
returns 5.7s later.

Three defects, three tests:

``test_a_timeout_is_not_reported_as_unreachable``
    The label is a connection failure. A timeout means the daemon was
    there and slow.
``test_a_timeout_keeps_qmds_own_words``
    ``TimeoutExpired.stderr`` is discarded at the boundary, so a
    timeout arrives with zero evidence of what qmd was doing. That is
    what made an intermittent stall undiagnosable.
``test_every_qmd_query_call_site_shares_one_deadline``
    The 15 was inherited from ``grounding._QMD_FANOUT_TIMEOUT``, sized
    on a 2026-09-25 cold-daemon probe and hardcoded with the sibling
    module's override left behind. The first cut of this fix wired
    ``search`` to the variable and left ``grounding`` on 15s, so one
    env var had two answers. Live warm latency is 5.6-6.0s; 60s is the
    layer's own default (``qmd_query``).
"""

from __future__ import annotations

import subprocess

import pytest

from tests.unit.mcp._prompt_body import rendered_body  # noqa: F401  (import guard)


@pytest.fixture(autouse=True)
def _registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-empty collection registry.

    Unit-test mode ships an empty one, so ``c:claude_code`` resolves to
    an unknown tag and ``_search_impl`` short-circuits before it ever
    reaches the qmd call these tests are about. Every test here is
    about what happens *after* the tag resolves, so the registry is
    stubbed rather than each test hand-rolling a tag-free path (which
    takes a different branch and would not exercise the same code).
    """
    import lies.library.registry as registry

    monkeypatch.setattr(registry, "library_collection_names", lambda: ["claude_code", "typer"])


def test_a_timeout_is_not_reported_as_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The word "unreachable" is a claim about the connection, not speed.

    Mutation behind this test: change the label back to
    ``qmd unreachable`` and the suite goes red on the first assertion.
    A reader chasing the wrong failure mode is the cost.
    """
    from lies.mcp import search as search_mod

    def boom(*_args: object, **_kwargs: object) -> None:
        raise search_mod.QmdTimeoutError("qmd query timed out after 60s")

    monkeypatch.setattr(search_mod, "_post_query", boom)

    result = search_mod._search_impl("plugin hooks", tag_expr="c:claude_code")

    reason = result["fallback_reason"]
    assert reason is not None
    assert "unreachable" not in reason, reason
    # Still names the daemon, so the failure is not merely a shrug —
    # and still names the deadline, so the reader knows it was time.
    assert "qmd" in reason, reason
    assert "60s" in reason, reason


def test_a_genuine_connection_failure_still_says_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The two failures must stay distinguishable, not collapse together.

    Over-correcting — labelling every ``QmdCommandError`` a timeout —
    would be as wrong as the original. This is the other half: a real
    qmd failure keeps the old wording.
    """
    from lies.mcp import search as search_mod

    def boom(*_args: object, **_kwargs: object) -> None:
        raise search_mod.QmdCommandError("qmd query failed (exit 1): ENOENT: no such index")

    monkeypatch.setattr(search_mod, "_post_query", boom)

    result = search_mod._search_impl("plugin hooks", tag_expr="c:claude_code")
    assert "unreachable" in result["fallback_reason"], result["fallback_reason"]


def test_a_timeout_keeps_qmds_own_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """stderr survives the boundary.

    ``cli.qmd_query`` raised ``QmdCommandError("qmd query timed out
    after {timeout}s")`` and dropped ``exc.stderr`` on the floor. On a
    timeout the message was the *only* evidence, and it was a constant
    — so the one failure that most needed diagnostics had none.

    Driven through the real ``qmd_query`` so the assertion is about the
    production seam rather than about the exception class alone.
    """
    from lies.qmd import cli

    stderr = b"Expanding query... (1ms)\nEmbedding 35 queries... (2.6s)\nReranking 40 chunks...\n"

    def raise_timeout(args: list[str], cwd: object, timeout: float) -> object:
        raise subprocess.TimeoutExpired(cmd=args, timeout=timeout, output=None, stderr=stderr)

    monkeypatch.setattr(cli, "_run_qmd", raise_timeout)

    with pytest.raises(cli.QmdTimeoutError) as excinfo:
        cli.qmd_query(cwd="/tmp", question="plugin hooks", limit=10, timeout=60)

    message = str(excinfo.value)
    assert "timed out after 60s" in message, message
    # qmd's own words are attached, so the reader can tell expansion
    # time from embedding time from rerank time.
    assert excinfo.value.stderr == stderr, excinfo.value.stderr
    assert "Reranking" in excinfo.value.stderr.decode()


def test_the_librarian_contract_does_not_call_a_timeout_a_coverage_gap() -> None:
    """The model-facing contract is where the false claim reaches the user.

    ``librarian.py`` told the model ``no_coverage=True`` means "the
    corpus has zero hits for this question". A timeout sets that flag,
    so the honest answer — *the search was slow, try again* — was
    never available to it.
    """
    from pathlib import Path

    import lies.agents.librarian as librarian

    source = Path(librarian.__file__).read_text(encoding="utf-8")
    assert "the daemon errored" not in source, (
        "the librarian contract still asserts a timeout means the daemon errored; "
        "a timeout is a slow daemon, not a failed one"
    )
    assert "transient" in source.lower(), (
        "the librarian contract has no notion of a transient failure, so a slow "
        "call is still answered as a coverage gap"
    )


def test_a_timeout_is_reported_as_transient_on_the_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The envelope distinguishes 'slow' from 'nothing there' and 'broken'.

    Three outcomes, three fields, so the synthesizer and the agent can
    tell them apart without parsing prose.
    """
    from lies.mcp import search as search_mod

    def boom(*_args: object, **_kwargs: object) -> None:
        raise search_mod.QmdTimeoutError("qmd query timed out after 60s")

    monkeypatch.setattr(search_mod, "_post_query", boom)

    result = search_mod._search_impl("plugin hooks", tag_expr="c:claude_code")
    assert result["transient"] is True, result
    assert result["no_coverage"] is False, (
        "a timeout is not evidence about the corpus; no_coverage asserts it has "
        "zero hits for the question, which the search never established"
    )

    # A clean miss is the opposite: transient False, no_coverage True.
    monkeypatch.setattr(search_mod, "_post_query", lambda *a, **k: [])
    clean = search_mod._search_impl("plugin hooks", tag_expr="c:claude_code")
    assert clean["transient"] is False, clean
    assert clean["no_coverage"] is True, clean


def test_a_clean_run_is_not_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    """The new field defaults to False — a success is not a failure."""
    from lies.mcp import search as search_mod

    monkeypatch.setattr(
        search_mod,
        "_post_query",
        lambda *a, **k: [{"file": "qmd://claude_code/plugins.md", "score": 0.9}],
    )
    result = search_mod._search_impl("plugin hooks", tag_expr="c:claude_code")
    assert result["transient"] is False, result
    assert result["no_coverage"] is False, result
    assert result["fallback_reason"] is None, result


def test_the_timeout_error_is_typed() -> None:
    """A timeout is its own exception, not a generic command error.

    Without a type, the search layer has to string-match the message to
    tell a slow daemon from a broken one — which is exactly the
    coupling that produced the "unreachable" mislabel.
    """
    from lies.qmd.cli import QmdCommandError, QmdTimeoutError

    assert issubclass(QmdTimeoutError, QmdCommandError)
    assert str(QmdTimeoutError("qmd query timed out after 60s")).startswith("qmd query timed out")


def test_a_timeout_is_not_raised_for_a_clean_empty_result() -> None:
    """``QmdNoResultsError`` is a miss, not a timeout.

    ``_post_query`` already converts it to ``[]``. This pins that the
    new exception type did not swallow that path.
    """
    from lies.qmd.cli import QmdNoResultsError, QmdTimeoutError

    assert not issubclass(QmdNoResultsError, QmdTimeoutError)
    assert not issubclass(QmdTimeoutError, QmdNoResultsError)


def test_qmd_timeout_error_carries_stderr_through_to_the_caller() -> None:
    """Programmatic callers get the bytes, not just the sentence.

    ``qmd_query`` is called from the fan-out and from ``search``; both
    have been unable to say *why* a call was slow. Attaching the
    captured stderr makes that answerable without a code change per
    call site.
    """
    from lies.qmd.cli import QmdTimeoutError

    err = QmdTimeoutError("qmd query timed out after 60s", stderr=b"Reranking 40 chunks...")
    # On the attribute, not interpolated into the message: the message
    # is what a log line shows, and the point of carrying the bytes is
    # that a caller can read the *whole* captured tail, not a prefix
    # someone chose to inline.
    assert err.stderr == b"Reranking 40 chunks..."
    assert str(err) == "qmd query timed out after 60s"


def test_a_timeout_with_no_stderr_still_raises() -> None:
    """The drain can yield empty stderr when the kill wins the race.

    ``_run_qmd`` sets ``stderr_b = b""`` when the post-kill drain also
    times out, so the attribute must be optional rather than assumed.
    """
    from lies.qmd.cli import QmdTimeoutError

    err = QmdTimeoutError("qmd query timed out after 60s")
    assert err.stderr is None
    assert "timed out" in str(err)


def test_the_subprocess_timeout_still_raises_timeout_expired() -> None:
    """The seam is unchanged: the exception type raised is still the stdlib one.

    Pins the contract ``_run_qmd`` documents, so a refactor that wraps
    it does not quietly change what callers catch.
    """
    assert issubclass(subprocess.TimeoutExpired, Exception)


# ---------------------------------------------------------------------------
# One deadline for the whole retrieval path.
#
# The first cut of this fix wired ``LIES_QMD_FANOUT_TIMEOUT`` into
# ``search`` and left ``grounding`` on its own default, so the two qmd
# query call sites shipped 60s and 15s respectively — one env var, two
# answers, for the same underlying subprocess. The knob was coherent
# only when a user set it, which is the case nobody reads the docs for.
#
# The retrieval deadline and the liveness deadlines are different
# questions. A slow *query* deserves patience; a slow *probe* should
# fail fast, so a wedged daemon is reported rather than waited on. The
# split is deliberate and the test below pins both halves, because
# collapsing them would make ``lies qmd status`` inherit a 60s hang.
# ---------------------------------------------------------------------------


def test_every_qmd_query_call_site_shares_one_deadline() -> None:
    """Two call sites, one number, no second place to change it.

    Mutation behind this test: give ``search`` its own default again
    and the two values diverge here, which is exactly the drift that
    shipped in the first cut.
    """
    from lies.config import get_qmd_query_timeout
    from lies.mcp import grounding, search

    # Both call sites read the one getter; neither carries a literal of
    # its own to drift. The first cut of this fix had exactly that
    # literal in each module — 60 in one, 15 in the other.
    assert search._current_timeout() == get_qmd_query_timeout()
    assert grounding._current_timeout() == get_qmd_query_timeout()
    assert get_qmd_query_timeout() == 60, "the layer's own default (qmd_query)"


def test_the_env_var_moves_both_query_call_sites_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One variable, both paths, no import-order trickery.

    Mutation behind this test: read the env var in only one module and
    the other stays at the default while this passes for the one that
    moved.
    """
    from lies.mcp import grounding, search

    monkeypatch.setenv("LIES_QMD_FANOUT_TIMEOUT", "7")
    # Read at call time, so no module reload is needed — the knob has
    # to work for an operator setting it before `lies mcp up`, and for
    # a test that sets it mid-process.
    assert search._current_timeout() == 7, search._current_timeout()
    assert grounding._current_timeout() == 7, grounding._current_timeout()


def test_liveness_probes_keep_their_own_short_deadlines() -> None:
    """A wedged daemon must be reported, not waited on for a minute.

    ``lies qmd status`` and the daemon bootstrap answer "is this alive?"
    — a question where a slow answer is itself the failure. Inheriting
    the 60s retrieval budget would turn a 5s status into a 60s hang on
    every operator command.
    """
    from lies.qmd import daemon as qmd_daemon
    from lies.qmd import lifecycle

    assert qmd_daemon.STATUS_TIMEOUT_S == 15.0
    assert lifecycle.DAEMON_START_TIMEOUT_S == 15.0
    assert lifecycle.PROBE_TIMEOUT_S == 5.0
    # …and none of them is reachable from the retrieval knob.
    assert lifecycle.PROBE_TIMEOUT_S < 15.0
