"""The qmd lock's facts reach the ``lib_ask`` envelope under REAL contention.

**What is real here, and why the earlier attempt at this test was
deleted.** A test named
``test_a_busy_qmd_lock_reaches_the_lib_ask_envelope`` used to call
``synth.synthesize`` — which no registered tool reaches — and
hand-build the ``QmdLockBusy``. It proved nothing about the shipped
ask path, so it was deleted. This file replaces it with a test that
runs the path a caller actually runs:

    lib_ask.fn  →  _ask_impl  →  librarian_agent_run
                →  librarian_agent (real Agent, real register_librarian_tools)
                →  _search closure  →  search.fn (_search_impl, real)
                →  access.daemon_tool("query")  →  qmd_lock_async
                →  _acquire_with_poll_async  →  QmdLockBusy
                ←  _search_impl's QmdLockBusy arm  →  SearchDiagnostics
                ←  with_search_diagnostics  →  LibrarianOutput  →  envelope

The contention is a **forked process holding the real lock inode**,
not a stubbed exception: the child runs the production
``with_qmd_lock`` decorator, writes the real pid sidecar and
heartbeat, and the parent's poll loop loses against it the way a
second ``lies qmd embed`` would.

**What is stubbed, and why it is not the subject.** Three things,
each of which is upstream of the lock gate or is the model:

1. ``search._validate_scope_blocking`` — a ``status`` call against the
   daemon's collection list. It is *not* gated by the lock, so leaving
   it real would dial a real address; the network audit is wired into
   ``make unit-test`` and would fail the session. Same stub, same
   reason, as ``tests/unit/mcp/test_search.py``.
2. The library registry — the isolated XDG has no collections, so
   ``_search_impl`` would answer "no collections registered" and never
   reach the query.
3. The librarian model — ``TestModel``, which is how you get a
   deterministic LLM. **The canned bundle is deliberately
   non-compliant**: ``no_coverage=True``, ``transient=False``,
   ``fallback_reason=None`` — i.e. the model claims the corpus has
   nothing *and* drops the reason. That is the failure the C1
   overlay exists to make impossible, so the test is only meaningful
   if the model is hostile.

The wait is on a stubbed clock (``_fake_poll_clock``) so a 120 s
budget costs no wall time. The budget's *arrival in the string* is
what is under test; its passage is not.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import os
import select
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

# Imported at module scope, deliberately. ``lies.mcp.synth`` and
# ``lies.agents.librarian`` are a few hundred milliseconds of import on
# a cold interpreter, and the per-test budget gate measures the *call*
# phase. Leaving them inside the test bodies charged that fixed cost to
# whichever test happened to run first — exactly the trap
# ``tests/unit/conftest.py`` documents when it removed the
# ``lies.qmd.capability`` pre-import. None of these modules read the
# library registry at import time, so importing them outside the
# per-test XDG isolation fixture is safe.
from lies.agents.librarian import LibrarianOutput  # noqa: E402
from lies.agents.librarian import librarian_agent  # noqa: E402
from lies.library import registry as reg_mod  # noqa: E402
from lies.library.registry import LibraryCollectionMeta  # noqa: E402
from lies.mcp import synth as synth_mod  # noqa: E402
from lies.mcp.search import _search_impl  # noqa: E402
from lies.qmd import lock as lock_mod  # noqa: E402

#: How long the parent waits for the forked holder to say it owns the
#: inode before declaring the harness broken. Generous: the child only
#: has to open a file and write a byte.
_HANDSHAKE_TIMEOUT_S = 10.0

#: These tests fork a real holder process. There is no Windows runner
#: today (Linux/WSL2 only) and no skip marker is strictly needed, but a
#: bare ``os.fork`` on Windows is an ``AttributeError`` in the test
#: body rather than a clean skip — and a red test on a platform nobody
#: runs is a worse signal than a skip. The precedent is weaker than it
#: looks: ``tests/unit/qmd/test_subprocess.py``'s fork lives inside a
#: generated child script's SOURCE STRING behind ``@pytest.mark.slow``,
#: so it neither runs by default nor fails at collection there.
#: Applied at module level because the fork is in a shared helper.
requires_fork = pytest.mark.skipif(
    not hasattr(os, "fork"),
    reason="os.fork is POSIX-only; the lock contention is a real child process",
)

#: Poll interval installed for the test. Raised from the production
#: 0.1 s so the *stubbed* 120 s clock needs ~20 poll attempts instead
#: of ~1200. Neither number is asserted on — the budget's arrival in
#: the rendered reason is — and each attempt is a real ``O_EXCL``
#: create against the real inode. ``test_the_async_sleep_is_clamped_
#: to_the_remaining_budget`` uses the same lever for the same reason.
_POLL_INTERVAL_S = 6.0


def _fake_poll_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Give ``qmd_lock_async``'s poll loop a clock it drives.

    ``_acquire_with_poll_async`` exits on ``time.monotonic()`` and
    delays on ``anyio.sleep``. Stub either one alone and the other
    still runs on real time — a real 120 s sleep, or a no-op sleep
    against a real deadline that spins for two minutes. Both come from
    shims installed on the ``lies.qmd.lock`` module, which resolves
    those names as module globals, so the swap is visible to the poll
    loop and to nothing else in the process.

    The clock advances one poll interval per sleep, so the iteration
    count is a function of ``query_lock_wait_s()`` and
    ``_POLL_INTERVAL_S`` rather than of how loaded the machine is.
    """
    state = {"now": 0.0, "sleeps": 0}
    poll_interval = lock_mod._POLL_INTERVAL_S

    class _AnyioShim:
        @staticmethod
        async def sleep(seconds: float, *a: Any, **kw: Any) -> None:
            state["sleeps"] += 1
            state["now"] += max(seconds, poll_interval)
            await asyncio.sleep(0)  # keep the loop fair; costs nothing

        def __getattr__(self, name: str) -> Any:
            import anyio

            return getattr(anyio, name)

    class _TimeShim:
        @staticmethod
        def monotonic() -> float:
            return state["now"]

        @staticmethod
        def time() -> float:
            import time

            return time.time()

        def __getattr__(self, name: str) -> Any:
            import time

            return getattr(time, name)

    monkeypatch.setattr(lock_mod, "anyio", _AnyioShim)
    monkeypatch.setattr(lock_mod, "time", _TimeShim)
    return state


@contextlib.contextmanager
def _real_lock_holder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Hold the qmd lock inode from a real forked process.

    The child runs the production ``with_qmd_lock`` decorator, so what
    the parent's poll loop loses against is the same create-lock +
    pid sidecar + heartbeat triple production contends with — not a
    hand-made file. That matters for the assertion: the holder PID in
    the rendered reason is a *different process's* PID, so "the pid
    crossed the boundary" is a real claim rather than a tautology
    about ``os.getpid()``.

    ``LIES_QMD_LOCK_PATH`` is pinned into ``tmp_path`` for the whole
    block. An earlier implementer on this branch polled the machine's
    real ``~/.local/state/lies/qmd.lock`` and left it held, which broke
    22 unrelated tests. Nothing outside this block can see this inode.
    """
    lock_path = tmp_path / "qmd.lock"
    monkeypatch.setenv("LIES_QMD_LOCK_PATH", str(lock_path))

    ready_r, ready_w = os.pipe()
    release_r, release_w = os.pipe()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - the child never returns
        os.close(ready_r)
        os.close(release_w)
        status = 0
        try:

            @lock_mod.with_qmd_lock(timeout_s=10.0)
            def _hold() -> None:
                os.write(ready_w, b"1")
                os.read(release_r, 1)

            _hold()
        except BaseException:
            status = 3
        finally:
            os._exit(status)

    os.close(ready_w)
    os.close(release_r)
    try:
        readable, _, _ = select.select([ready_r], [], [], _HANDSHAKE_TIMEOUT_S)
        if not readable:
            pytest.fail(
                f"the forked holder never took {lock_path} within "
                f"{_HANDSHAKE_TIMEOUT_S}s; the contention this test measures "
                f"was never established"
            )
        if os.read(ready_r, 1) != b"1":
            pytest.fail(
                "the forked holder exited without acquiring the lock; the "
                "child failed before it could signal (exit status is in the "
                "pytest output)"
            )
        yield lock_path
    finally:
        with contextlib.suppress(OSError):
            os.write(release_w, b"1")
        with contextlib.suppress(OSError):
            os.close(release_w)
        with contextlib.suppress(ChildProcessError, OSError):
            os.waitpid(pid, 0)
        with contextlib.suppress(OSError):
            os.close(ready_r)


def _seed_one_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    """One registered collection, and no daemon scope pre-check.

    Both are the two hops *before* the lock gate, and both have to be
    stubbed for the same reason. The isolated XDG has no collections, so
    ``_search_impl`` answers "no collections registered" and never
    reaches the query at all; and the pre-check is a ``status`` call
    against the daemon, which the lock does not gate, so leaving it
    real would dial a real address — and the network audit is wired
    into ``make unit-test``. Same stub, same reason, as
    ``tests/unit/mcp/test_search.py``'s ``_bypass_daemon_precheck``.
    """
    metas = [LibraryCollectionMeta(name="alpha", tags=("plugins",))]
    monkeypatch.setattr(reg_mod, "library_collection_names", lambda: frozenset({"alpha"}))
    monkeypatch.setattr(reg_mod, "library_collection_metas", lambda: iter(metas))
    monkeypatch.setattr("lies.mcp.search._validate_scope_blocking", lambda scope: (list(scope), []))


def _noncompliant_librarian() -> TestModel:
    """A ``TestModel`` whose bundle contradicts what the search found.

    ``no_coverage=True`` is the bundle's claim that the corpus has
    nothing for the question, and ``fallback_reason=None`` drops the
    reason the tool handed over. Both are what a model under output
    pressure emits, and both are wrong here: nothing was dispatched,
    so nothing is known about the corpus.
    """
    return TestModel(
        call_tools=["search"],
        custom_output_args=dataclasses.asdict(
            LibrarianOutput(
                tag_expr=None,
                exclude_expr=None,
                excerpts=[],
                distinct_pages=0,
                no_coverage=True,
                searched_scope=[],
            )
        ),
    )


@requires_fork
def test_the_ask_path_names_the_qmd_lock_holder_under_real_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every fact the operator needs survives a genuine lock wait-out.

    The assertion that matters is the *model's*: the canned bundle says
    the corpus has nothing and carries no reason, and the envelope still
    says "inconclusive" and names the holder. Before the
    ``SearchDiagnostics`` overlay the bundle's own fields were the
    whole account, so this test fails on the ``fallback_reason`` and
    ``answer`` assertions and not on the lock machinery at all.
    """
    with _real_lock_holder(monkeypatch, tmp_path) as lock_path:
        monkeypatch.setattr(lock_mod, "_POLL_INTERVAL_S", _POLL_INTERVAL_S)
        clock = _fake_poll_clock(monkeypatch)
        _seed_one_collection(monkeypatch)

        # The model. Deliberately hostile — see `_noncompliant_librarian`.
        monkeypatch.setattr(
            synth_mod,
            "librarian_agent",
            lambda model=None: librarian_agent(model=_noncompliant_librarian()),
        )
        monkeypatch.setattr(synth_mod, "_resolve_librarian_model", lambda: "test-model")
        synthesizer_calls: list[str] = []
        monkeypatch.setattr(
            synth_mod,
            "synthesizer_agent_run",
            lambda lib_out, q: synthesizer_calls.append(q) or None,
        )

        # Capture the bundle ``librarian_agent_run`` hands back.
        # ``_ask_impl`` reads ``transient`` first and never reads
        # ``no_coverage`` at all, so the envelope alone cannot witness
        # the flag. Nor can any other in-tree consumer: the
        # orchestrator's ``run_query`` recomputes ``answer.no_coverage``
        # from the resolved tag scope (``orchestrator.py``'s
        # ``no_coverage = not answer.searched_scope and ...``), never
        # from the bundle, and the synthesizer's ``QueryDeps`` renders
        # only ``librarian_output.excerpts`` via ``page_texts`` /
        # ``page_sources``. So the return value of
        # ``librarian_agent_run`` is the only place on the registered
        # path where the overlay's ``no_coverage`` decision is
        # observable, and that is why the spy is here rather than at
        # the envelope.
        bundles: list[LibrarianOutput] = []
        real_librarian_agent_run = synth_mod.librarian_agent_run

        def _spy(deps: object) -> LibrarianOutput:
            bundle = real_librarian_agent_run(deps)
            bundles.append(bundle)
            return bundle

        monkeypatch.setattr(synth_mod, "librarian_agent_run", _spy)

        out = synth_mod.lib_ask.fn(question="what is pydantic?", tag_expr=None)

    assert len(bundles) == 1, f"the librarian ran {len(bundles)} times, not once"
    bundle = bundles[0]
    reason = out.fallback_reason or ""

    # The bundle itself, as every consumer of it sees it. The model
    # emitted ``no_coverage=True`` and ``fallback_reason=None``; both
    # are wrong, because nothing was dispatched.
    assert bundle.no_coverage is False, (
        "the search never dispatched, so the bundle cannot claim the corpus "
        "has nothing. no in-tree consumer reads this field today (the "
        "envelope's transient branch is taken first, the orchestrator "
        "recomputes its own, the synthesizer reads only excerpts), so this "
        "is the bundle keeping its own contract honest rather than the "
        "pin protecting a live reader."
    )
    assert bundle.transient is True
    assert (bundle.fallback_reason or "") == reason, (
        "the envelope renders the bundle's reason verbatim; they must be "
        f"the same string, got {bundle.fallback_reason!r} vs {reason!r}"
    )

    # The gate actually ran, and actually waited: a real contended
    # acquire against a real foreign holder.
    assert clock["sleeps"] > 1, (
        "the poll loop ran once or not at all, so no real contention was "
        f"measured (sleeps={clock['sleeps']})"
    )
    assert str(lock_path) in reason, f"the contended inode must reach the reader; got {reason!r}"

    # Not a corpus claim, and not the "the library has nothing" claim
    # the non-compliant model asserted.
    assert "inconclusive" in out.answer, out.answer
    assert "No relevant content found in library." not in out.answer, (
        "a search that never dispatched cannot be reported as an empty corpus"
    )
    assert out.fallback_used is True
    assert out.synthesis_used is False
    assert synthesizer_calls == [], "the synthesizer must not run on a failed dispatch"

    # The scope the tool resolved survives too — same overlay, same
    # reason.
    assert out.searched_scope == ["alpha"], out.searched_scope


@requires_fork
def test_the_rendered_reason_names_the_holder_pid_and_the_real_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pid, the wait and *the lock budget* — not the query timeout.

    Two budgets exist and they are not equal: ``query_lock_wait_s()``
    (120 s by default) is what the gate waited on, and
    ``get_qmd_query_timeout()`` (60 s by default) is the daemon read
    deadline, which never expired here. An earlier version of
    ``_search_impl``'s ``QmdLockBusy`` arm rendered the second inside
    "timed out after Ns", printing "60s" beside "waited 120.0s, max
    120.0s" in one sentence. The reader was handed two budgets and the
    leading one described a deadline that had not fired.

    Asserted against the number the gate actually used, with
    ``LIES_QMD_QUERY_LOCK_WAIT`` left at its default, so this pins the
    shipped 120 s rather than a fixture's.
    """
    monkeypatch.delenv("LIES_QMD_QUERY_LOCK_WAIT", raising=False)

    with _real_lock_holder(monkeypatch, tmp_path) as lock_path:
        monkeypatch.setattr(lock_mod, "_POLL_INTERVAL_S", _POLL_INTERVAL_S)
        _fake_poll_clock(monkeypatch)
        _seed_one_collection(monkeypatch)

        envelope = _search_impl("what is pydantic?")

        # Read the holder pid back off the inode *while it is still
        # held* — the sidecar is unlinked on release. The child's pid
        # is not this process's, so asserting the number in the string
        # is a real cross-process claim rather than a tautology.
        holder_pid = int(Path(f"{lock_path}.pid").read_text(encoding="utf-8").strip())
        assert holder_pid != os.getpid(), (
            "the holder must be another process, or 'the pid reached the "
            "reader' is only a statement about os.getpid()"
        )

    reason = envelope["fallback_reason"] or ""

    assert envelope["transient"] is True
    assert envelope["no_coverage"] is False, "a queued-out dispatch is never a corpus claim"
    assert envelope["hits"] == []

    assert "queued behind another qmd operation" in reason, reason
    assert "wait and retry" in reason, reason
    assert str(lock_path) in reason, reason
    # Positive, not "60s not in reason". The reason embeds a
    # machine-generated tmp_path, so a base-temp containing "60s" would
    # fail a negative assertion for a reason that has nothing to do with
    # the bug under test. This form is strictly stronger anyway: it names
    # the budget, the unit and the wording together.
    assert "gave up after 120.0s" in reason, (
        f"the budget the gate actually waited on must be the one rendered; got {reason!r}"
    )
    assert str(holder_pid) in reason, (
        f"the holder pid {holder_pid} must reach the reader; got {reason!r}"
    )


def _dies_after_searching() -> FunctionModel:
    """A librarian model that searches, then dies before answering.

    Turn 1 returns a ``search`` tool call; the closure really runs and
    really records. Turn 2 raises, standing in for the two production
    ways ``run_sync`` raises after tools have already executed —
    ``UsageLimitExceeded`` when the output cannot be validated within
    the retry budget, and a transport failure mid-run. Both leave the
    recorded search envelope intact, which is the whole point: the work
    that already happened is not un-happened by the failure after it.
    """
    turns = {"n": 0}

    def _model(messages: list, info: AgentInfo) -> ModelResponse:
        turns["n"] += 1
        if turns["n"] == 1:
            return ModelResponse(parts=[ToolCallPart("search", {"question": "what is pydantic?"})])
        raise RuntimeError("Exceeded maximum output retries (1)")

    model = FunctionModel(_model)
    model.lies_turns = turns  # type: ignore[attr-defined]
    return model


@requires_fork
def test_a_model_that_dies_after_a_queued_search_still_names_the_holder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatch-failure branch is where the overlay earns its keep.

    ``run_sync`` raising is the *only* situation the overlay changes
    the outcome for. On the success path the bundle comes from the
    model, which at least has the tool's envelope in its context; when
    ``run_sync`` raises there is no bundle at all and the caller gets a
    fixed wrapper string. Without the overlay on this branch the
    operator reads

        librarian dispatch failed: RuntimeError: Exceeded maximum
        output retries (1)

    and loses the holder pid, the elapsed wait, the budget and the
    contended inode — the one fact that says "wait and retry" instead
    of "your model is broken". The search already ran; the failure came
    after it.

    **This pin was missing and the claim was not.** An earlier draft
    asserted in prose that the overlay covers both branches, and
    deleting the wrapper from this one passed the whole unit suite at
    rc=0 — because the sibling tests stub ``register_librarian_tools``
    to a no-op, so ``diagnostics`` is empty and the overlay is a
    no-op with or without it. This test drives a real agent with real
    tools against real lock contention, so the record is non-empty and
    the branch is the only thing that can supply the reason.

    Same real contention as the tests above, for the same reason: a
    hand-built ``QmdLockBusy`` would make this a restatement of them.
    """
    with _real_lock_holder(monkeypatch, tmp_path) as lock_path:
        monkeypatch.setattr(lock_mod, "_POLL_INTERVAL_S", _POLL_INTERVAL_S)
        _fake_poll_clock(monkeypatch)
        _seed_one_collection(monkeypatch)

        dying_model = _dies_after_searching()
        monkeypatch.setattr(
            synth_mod,
            "librarian_agent",
            # ``model=None`` shadows nothing: the closure body names
            # ``dying_model``, so the resolved string never reaches
            # ``librarian_agent``. A lambda that passed its own
            # ``model`` through would build an agent from "test-model".
            lambda model=None: librarian_agent(model=dying_model),
        )
        monkeypatch.setattr(synth_mod, "_resolve_librarian_model", lambda: "test-model")
        synthesizer_calls: list[str] = []
        monkeypatch.setattr(
            synth_mod,
            "synthesizer_agent_run",
            lambda lib_out, q: synthesizer_calls.append(q) or None,
        )

        out = synth_mod.lib_ask.fn(question="what is pydantic?", tag_expr=None)
        holder_pid = int(Path(f"{lock_path}.pid").read_text(encoding="utf-8").strip())

    # The search really ran before the model died. Without this the
    # test could pass against an empty record for the wrong reason.
    turns = dying_model.lies_turns["n"]  # type: ignore[attr-defined]
    assert turns == 2, f"the model should have searched then died; it took {turns} turn(s)"
    assert holder_pid != os.getpid()

    reason = out.fallback_reason or ""
    # The facts, not the wrapper.
    assert "Exceeded maximum output retries" not in reason, (
        f"the wrapper string is what the overlay exists to replace on this branch; got {reason!r}"
    )
    assert str(holder_pid) in reason, f"the holder pid must survive; got {reason!r}"
    assert str(lock_path) in reason, f"the contended inode must survive; got {reason!r}"
    assert "gave up after 120.0s" in reason, reason

    # Still a run claim, never a corpus claim, and the synthesizer is
    # not reached.
    assert "inconclusive" in out.answer, out.answer
    assert "No relevant content found in library." not in out.answer, out.answer
    assert out.synthesis_used is False
    assert synthesizer_calls == []


# ---------------------------------------------------------------------------
# The overlay's own boundaries, on the dataclass directly
# ---------------------------------------------------------------------------
#
# The end-to-end tests above prove the happy path of the overlay. These
# pin its *edges*, which is where an over-eager overlay becomes its own
# kind of lie: a bundle assembled from real page bodies is evidence, and
# discarding it because an earlier attempt queued would be a different
# false claim. The other edge is the mirror — a reason recorded against
# a dispatch that DID run must not flip a corpus statement into a run
# statement, because then the overlay would be manufacturing the very
# false claim this branch exists to remove.


def test_an_evidence_bundle_is_never_overwritten() -> None:
    """Excerpts win over a recorded failure.

    The librarian contract tells the model to retry a transient search
    once. When that retry succeeds, the bundle is assembled from real
    ``read`` bodies — and forcing ``transient`` onto it would make
    ``_ask_impl`` throw the whole thing away and report "inconclusive"
    for an answer it already has. The record deliberately keeps the
    first failure (see :meth:`SearchDiagnostics.record`); this is where
    that decision is bounded.
    """
    from lies.agents.librarian import (
        LibrarianOutput,
        PageExcerpt,
        SearchDiagnostics,
        with_search_diagnostics,
    )
    from lies.markdown_spans import Span

    excerpt = PageExcerpt(
        collection="alpha",
        slug="alpha/page.md",
        title="Page",
        spans=[Span(heading_path=["H"], body="verbatim", code_fence=False, start_line=1)],
    )
    bundle = LibrarianOutput(
        tag_expr=None,
        exclude_expr=None,
        excerpts=[excerpt],
        distinct_pages=1,
        no_coverage=False,
        searched_scope=["alpha"],
    )
    diag = SearchDiagnostics(transient=True, fallback_reason="queued behind PID 4242")

    out = with_search_diagnostics(bundle, diag)

    assert out is bundle, "an evidence-bearing bundle must come back untouched"
    assert out.transient is False
    assert out.no_coverage is False
    assert out.fallback_reason is None, (
        "attaching a reason to a bundle that has evidence would render it "
        "as a failure the caller is told to act on"
    )


def test_a_recorded_failure_survives_a_later_clean_search() -> None:
    """A retry that succeeds does not erase the wait that already happened.

    The librarian is told to retry a transient search once. Recording
    only the *last* envelope would let a successful retry silently
    retire a lock-busy that consumed the whole 120 s budget — and the
    caller would render it as an ordinary answer rather than as a run
    that spent its budget queueing. The record is sticky for exactly
    that reason, and this is the assertion that keeps it sticky.
    """
    from lies.agents.librarian import SearchDiagnostics

    diag = SearchDiagnostics()
    diag.record(
        {
            "transient": True,
            "fallback_reason": "queued behind PID 4242, waited 120.0s, max 120.0s",
            "searched_scope": ["alpha"],
        }
    )
    diag.record(
        {
            "transient": False,
            "fallback_reason": None,
            "searched_scope": ["alpha", "beta"],
        }
    )

    assert diag.transient is True, (
        "a clean retry must not retire the failure that preceded it; the "
        "120s budget was already spent queueing and that is the fact the "
        "operator needs"
    )
    assert diag.fallback_reason is not None
    assert "4242" in diag.fallback_reason
    # The scope, by contrast, is the latest truth: it describes what
    # the search most recently asked and what the daemon served.
    assert diag.searched_scope == ["alpha", "beta"]


def test_a_non_transient_reason_does_not_flip_a_corpus_statement() -> None:
    """A dispatch that RAN keeps its ``no_coverage``.

    ``_search_impl`` writes a reason for outcomes that are not process
    failures: an unknown tag, a tag expression resolving to nothing, no
    collections registered, a collection registered but empty in qmd.
    Those are genuine statements about the request and the index, and
    the librarian's ``no_coverage`` is the model's reading of them. The
    overlay replaces the *reason* — verbatim, which is strictly more
    informative than the model's paraphrase — but must leave both flags
    alone.

    Without the ``if diag.transient`` guard, ``no_coverage`` would be
    forced ``False`` on every one of those paths, and the bundle would
    report "we learned nothing" for a search that ran and found
    nothing. That is the same false-claim shape this branch exists to
    remove, manufactured by the fix for it.
    """
    from lies.agents.librarian import (
        LibrarianOutput,
        SearchDiagnostics,
        with_search_diagnostics,
    )

    for reason in (
        "unknown tag: 'c:nope'",
        "tag expression resolved to no collections: 'c:nope'",
        "collection 'alpha' is registered but has no documents in qmd",
    ):
        bundle = LibrarianOutput(
            tag_expr=None,
            exclude_expr=None,
            excerpts=[],
            distinct_pages=0,
            no_coverage=True,
            searched_scope=["alpha"],
        )
        diag = SearchDiagnostics(transient=False, fallback_reason=reason)

        out = with_search_diagnostics(bundle, diag)

        assert out.no_coverage is True, (
            f"a reason against a dispatch that RAN must not clear no_coverage: {reason!r}"
        )
        assert out.transient is False, reason
        # The reason itself IS replaced — that half still applies.
        assert out.fallback_reason == reason


def test_a_bundle_transient_flag_survives_a_non_transient_recorded_reason() -> None:
    """The two flags merge with ``or``; the record never clears the model.

    A bundle can be transient for a reason the search tool knows nothing
    about — every read failing, the model refusing to continue. That
    verdict is the model's own and there is no recorded failure to
    contradict it, so a ``diag.transient`` that is ``False`` must not
    demote it. Asserted because the alternative (``transient =
    diag.transient``) is a one-character edit that silently converts
    "inconclusive" into "No relevant content found in library." for any
    bundle assembled without a recorded dispatch failure.
    """
    from lies.agents.librarian import (
        LibrarianOutput,
        SearchDiagnostics,
        with_search_diagnostics,
    )

    bundle = LibrarianOutput(
        tag_expr=None,
        exclude_expr=None,
        excerpts=[],
        distinct_pages=0,
        no_coverage=False,
        searched_scope=["alpha"],
        transient=True,
        fallback_reason="every read failed",
    )
    diag = SearchDiagnostics(transient=False, fallback_reason="unknown tag: 'c:nope'")

    out = with_search_diagnostics(bundle, diag)

    assert out.transient is True, (
        "the record has nothing to say about a bundle the model marked "
        "transient for its own reason; it must not demote the verdict"
    )
    assert out.fallback_reason == "unknown tag: 'c:nope'"
