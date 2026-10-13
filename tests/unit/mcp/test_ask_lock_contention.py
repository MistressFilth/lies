"""The qmd lock's facts reach the ``lib_ask`` envelope under REAL contention."""

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

# Imported at module scope, deliberately: the per-test budget gate
# measures the *call* phase, so an import inside a test body would be
# charged to whichever test ran first. None of these read the library
# registry at import time.
from lies.agents.librarian import LibrarianOutput  # noqa: E402
from lies.agents.librarian import librarian_agent  # noqa: E402
from lies.library import registry as reg_mod  # noqa: E402
from lies.library.registry import LibraryCollectionMeta  # noqa: E402
from lies.mcp import synth as synth_mod  # noqa: E402
from lies.mcp.search import _search_impl  # noqa: E402
from lies.qmd import lock as lock_mod  # noqa: E402

#: How long the parent waits for the forked holder to claim the inode.
_HANDSHAKE_TIMEOUT_S = 10.0

#: Module level because the fork is in a shared helper.
requires_fork = pytest.mark.skipif(
    not hasattr(os, "fork"),
    reason="os.fork is POSIX-only; the lock contention is a real child process",
)

#: Poll interval installed for the test. Raised from the production
#: 0.1 s so the *stubbed* 120 s clock needs ~20 poll attempts instead
#: of ~1200. Neither number is asserted on — the budget's arrival in
#: the rendered reason is — and each attempt is a real ``O_EXCL``
#: create against the real inode.
_POLL_INTERVAL_S = 6.0


def _fake_poll_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Give ``qmd_lock_async``'s poll loop a clock it drives."""
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
    """Hold the qmd lock inode from a real forked process."""
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
    """One registered collection, and no daemon scope pre-check."""
    metas = [LibraryCollectionMeta(name="alpha", tags=("plugins",))]
    monkeypatch.setattr(reg_mod, "library_collection_names", lambda: frozenset({"alpha"}))
    monkeypatch.setattr(reg_mod, "library_collection_metas", lambda: iter(metas))
    monkeypatch.setattr("lies.mcp.search._validate_scope_blocking", lambda scope: (list(scope), []))


def _noncompliant_librarian() -> TestModel:
    """A ``TestModel`` whose bundle contradicts what the search found."""
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
    """Every fact the operator needs survives a genuine lock wait-out."""
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

        # Spy at ``librarian_agent_run``, not at the envelope: ``_ask_impl``
        # reads ``transient`` and never reads ``no_coverage``, so the
        # overlay's ``no_coverage`` decision is only observable here.
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
    """The pid, the wait and *the lock budget* — not the query timeout."""
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
    """A librarian model that searches, then dies before answering."""
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
    """The dispatch-failure branch is where the overlay earns its keep."""
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
# Pins the overlay's *edges*: an evidence-bearing bundle must survive a
# recorded failure, and a reason recorded against a dispatch that DID run
# must not flip a corpus statement into a run statement.


def test_an_evidence_bundle_is_never_overwritten() -> None:
    """Excerpts win over a recorded failure."""
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
    diag.record_read({"alpha/page.md": "# Page\n\nverbatim"})

    out = with_search_diagnostics(bundle, diag)

    assert out is bundle, "an evidence-bearing bundle must come back untouched"
    assert out.transient is False
    assert out.no_coverage is False
    assert out.fallback_reason is None, (
        "attaching a reason to a bundle that has evidence would render it "
        "as a failure the caller is told to act on"
    )


def test_an_evidence_bundle_with_no_recorded_source_is_not_evidence() -> None:
    """The other side of the rule, and the one the fixture above could hide."""
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

    assert out.excerpts == [], (
        "an excerpt for a path no tool returned must not survive; the "
        f"bundle still carries it: {[e.slug for e in out.excerpts]}"
    )
    # A run claim, never a corpus claim — the whole bundle was invented,
    # so nothing about the library was learned.
    assert out.no_coverage is False
    assert out.transient is True
    reason = out.fallback_reason or ""
    assert "alpha/page.md" in reason, reason
    assert "4242" in reason, (
        f"the recorded lock fact must survive alongside the drop; got {reason!r}"
    )


def test_a_recorded_failure_survives_a_later_clean_search() -> None:
    """A retry that succeeds does not erase the wait that already happened."""
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
    """A dispatch that RAN keeps its ``no_coverage``."""
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
    """The two flags merge with ``or``; the record never clears the model."""
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
