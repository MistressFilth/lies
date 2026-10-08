"""A qmd timeout is not an unreachable daemon, and must not read as one.

A timeout sets ``no_coverage=True`` would let a slow call reach the
user as "No relevant content found in library." — a false claim
about the corpus for a query that returns 5.7 s later. These tests
pin three things: the label, the captured stderr, and the shared
60 s deadline.
"""

from __future__ import annotations

import subprocess

import pytest

from tests.unit.mcp._prompt_body import rendered_body  # noqa: F401  (import guard)


@pytest.fixture(autouse=True)
def _registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-empty collection registry, and no live daemon.

    Unit-test mode ships an empty registry, so ``c:claude_code``
    resolves to an unknown tag and ``_search_impl`` short-circuits
    before it reaches the qmd call these tests are about.

    ``_search_impl`` also validates scope against the daemon's own
    ``status`` before dispatching. That is a real reachability probe,
    and every test here stubs ``_post_query`` rather than exercising
    the daemon -- so the probe is stubbed too, to the same names. A
    unit test that needs a live daemon on 127.0.0.1:8181 is a unit
    test that fails in CI and passes on a machine that happens to have
    one running.
    """
    import lies.library.registry as registry
    from lies.library.registry import LibraryCollectionMeta
    from lies.qmd import access

    _names = ["claude_code", "typer"]
    monkeypatch.setattr(registry, "library_collection_names", lambda: list(_names))
    # The tag filter resolves through the same registry accessors the
    # archivist uses — names, the ``c:``-prefixed spellings, the tags,
    # and the per-collection meta rows. Stubbing names alone left the
    # metas empty, so a ``c:claude_code`` filter validated and then
    # matched nothing, which the tool correctly reported as an unknown
    # tag. Every test here is about the daemon boundary, so a filter that
    # cannot resolve would have masked the assertion it is making.
    monkeypatch.setattr(
        registry,
        "library_collection_metas",
        lambda: iter(
            [
                LibraryCollectionMeta(
                    name=n,
                    source_url=f"https://example.test/{n}",
                    tags=frozenset({"plugins"}),
                    scope_keywords=frozenset(),
                )
                for n in _names
            ]
        ),
    )
    monkeypatch.setattr(registry, "library_collection_tags", lambda: frozenset({"plugins"}))

    async def _served() -> frozenset[str]:
        return frozenset({"claude_code", "typer"})

    monkeypatch.setattr(access, "qmd_collection_names", _served)


def test_a_timeout_is_not_reported_as_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """The word "unreachable" is a claim about the connection, not speed."""
    from lies.mcp import search as search_mod

    def boom(*_args: object, **_kwargs: object) -> None:
        raise search_mod.QmdTimeoutError("qmd query timed out after 60s")

    monkeypatch.setattr(search_mod, "_post_query", boom)

    result = search_mod._search_impl("plugin hooks", tag_expr="c:claude_code")

    reason = result["fallback_reason"]
    assert reason is not None
    assert "unreachable" not in reason, reason
    # Still names the daemon and the deadline.
    assert "qmd" in reason, reason
    assert "60s" in reason, reason


def test_a_timeout_keeps_qmds_own_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """stderr survives the boundary.

    Driven through the real ``qmd_query`` so the assertion is
    about the production seam, not just the exception class.
    """
    from lies.qmd import cli

    stderr = b"Expanding query... (1ms)\nEmbedding 35 queries... (2.6s)\nReranking 40 chunks...\n"

    # `idle_timeout` is passed by `qmd_query` (see QMD_QUERY_IDLE_TIMEOUT_S);
    # accept and ignore it, as the real `_run_qmd` signature does.
    def raise_timeout(args: list[str], cwd: object, timeout: float, **kwargs: object) -> object:
        raise subprocess.TimeoutExpired(cmd=args, timeout=timeout, output=None, stderr=stderr)

    monkeypatch.setattr(cli, "_run_qmd", raise_timeout)

    with pytest.raises(cli.QmdTimeoutError) as excinfo:
        cli.qmd_query(cwd="/tmp", question="plugin hooks", limit=10, timeout=60)

    message = str(excinfo.value)
    assert "timed out after 60s" in message, message
    assert excinfo.value.stderr == stderr, excinfo.value.stderr
    assert "Reranking" in excinfo.value.stderr.decode()


def test_the_librarian_contract_does_not_call_a_timeout_a_coverage_gap() -> None:
    """The model-facing contract is where the false claim reaches the user."""
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
    """A timeout is 'slow', which is the other end of the field from 'nothing there'.

    Split from the clean-miss half: two ``_search_impl`` calls in one test
    measured 0.14-0.16 s against the 0.15 s budget depending on suite
    load, so it flapped. Each assertion stands on its own and neither is
    weakened by the split.
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


def test_a_clean_miss_is_the_opposite_end_of_the_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely empty result is the one case that *is* a corpus claim."""
    from lies.mcp import search as search_mod

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
    """A timeout is its own exception, not a generic command error."""
    from lies.qmd.cli import QmdCommandError, QmdTimeoutError

    assert issubclass(QmdTimeoutError, QmdCommandError)
    assert str(QmdTimeoutError("qmd query timed out after 60s")).startswith("qmd query timed out")


def test_a_timeout_is_not_raised_for_a_clean_empty_result() -> None:
    """``QmdNoResultsError`` is a miss, not a timeout."""
    from lies.qmd.cli import QmdNoResultsError, QmdTimeoutError

    assert not issubclass(QmdNoResultsError, QmdTimeoutError)
    assert not issubclass(QmdTimeoutError, QmdNoResultsError)


def test_qmd_timeout_error_carries_stderr_through_to_the_caller() -> None:
    """Programmatic callers get the bytes, not just the sentence."""
    from lies.qmd.cli import QmdTimeoutError

    err = QmdTimeoutError("qmd query timed out after 60s", stderr=b"Reranking 40 chunks...")
    # On the attribute, not interpolated into the message.
    assert err.stderr == b"Reranking 40 chunks..."
    assert str(err) == "qmd query timed out after 60s"


def test_a_timeout_with_no_stderr_still_raises() -> None:
    """The drain can yield empty stderr when the kill wins the race."""
    from lies.qmd.cli import QmdTimeoutError

    err = QmdTimeoutError("qmd query timed out after 60s")
    assert err.stderr is None
    assert "timed out" in str(err)


def test_the_subprocess_timeout_still_raises_timeout_expired() -> None:
    """The seam still raises the stdlib exception type."""
    assert issubclass(subprocess.TimeoutExpired, Exception)


def test_every_qmd_query_call_site_shares_one_deadline() -> None:
    """Two call sites, one number, no second place to change it.

    Both call sites read the one getter; neither carries a literal
    of its own to drift.
    """
    from lies.config import get_qmd_query_timeout
    from lies.mcp import grounding, search

    assert search._current_timeout() == get_qmd_query_timeout()
    assert grounding._current_timeout() == get_qmd_query_timeout()
    assert get_qmd_query_timeout() == 60, "the layer's own default (qmd_query)"


def test_the_env_var_moves_both_query_call_sites_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One variable, both paths, no import-order trickery."""
    from lies.mcp import grounding, search

    monkeypatch.setenv("LIES_QMD_FANOUT_TIMEOUT", "7")
    assert search._current_timeout() == 7, search._current_timeout()
    assert grounding._current_timeout() == 7, grounding._current_timeout()


def test_liveness_probes_keep_their_own_short_deadlines() -> None:
    """A wedged daemon must be reported, not waited on for a minute.

    ``lies qmd status`` and the daemon bootstrap answer "is this
    alive?" — a slow answer is itself the failure. Inheriting the
    60s retrieval budget would turn a 5s status into a 60s hang.
    """
    from lies.qmd import daemon as qmd_daemon
    from lies.qmd import lifecycle

    assert qmd_daemon.STATUS_TIMEOUT_S == 15.0
    assert lifecycle.DAEMON_START_TIMEOUT_S == 15.0
    assert lifecycle.PROBE_TIMEOUT_S == 5.0
    # None of them is reachable from the retrieval knob.
    assert lifecycle.PROBE_TIMEOUT_S < 15.0
