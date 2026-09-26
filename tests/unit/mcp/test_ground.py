"""Tests for src/lies/mcp/grounding.py — Task 1 fan-out wiring.

Pins the unscoped-query brick-wall fix: ``ArchivistDigest.no_library``
additive field, ``_fanout_unscoped()`` parallel dispatcher, and the
``no_library=True`` fast-path when no library collections are
registered. The fan-out path replaces the F18 librarian LLM round-trip
on unscoped queries (which timed out at ~42s / returned 0 citations
per session 2505630b's reproduction) with direct qmd fan-out across
registered library collections.
"""

from __future__ import annotations

import asyncio
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _silence_wiring_skipped_warning() -> None:
    """Silence the ``ground: tool wiring skipped`` warning by default."""
    warnings.filterwarnings(
        "ignore",
        message=r"^ground: tool wiring skipped\b",
        category=UserWarning,
    )


@pytest.fixture(autouse=True)
def _reset_consecutive_qmd_errors() -> None:
    """Reset the recycle-trigger counter between tests.

    The counter lives at module scope on ``lies.mcp.grounding`` so a
    wedged-daemon simulation can persist across the fan-out (the
    recycle path expects exactly that). Without this fixture the
    second test in the file sees a counter residue from the first —
    autouse yields both pre-test and post-test resets so adjacent
    tests stay hermetic regardless of order.
    """
    from lies.mcp import grounding

    grounding._consecutive_qmd_errors = 0
    yield
    grounding._consecutive_qmd_errors = 0


def test_archivist_digest_has_no_library_field_default_false() -> None:
    """`no_library` defaults to False for back-compat with existing call sites."""
    from lies.mcp.grounding import ArchivistDigest

    digest = ArchivistDigest(
        question="q",
        tag_expr=None,
        exclude_expr=None,
        citations=[],
        no_coverage=True,
        distinct_pages=0,
        searched_scope=[],
    )
    assert digest.no_library is False


def test_ground_unscoped_uses_fanout(monkeypatch) -> None:
    """Unscoped ground() calls _fanout_unscoped and merges results."""
    from lies.markdown_spans import Span
    from lies.mcp import grounding
    from lies.query import synthesizer as synth_mod

    # Make the registry appear populated so the ``no_library=True``
    # fast-path is bypassed and the fan-out branch fires.
    monkeypatch.setattr(synth_mod, "_all_collection_names", lambda: ["switchyard"])

    @dataclass(frozen=True)
    class _FakeExcerpt:
        collection: str
        slug: str
        title: str
        spans: list = field(default_factory=list)
        source_kind: str = "library"

    fake_excerpts = [
        _FakeExcerpt(
            collection="switchyard",
            slug="switchyard/install.md",
            title="Install",
            spans=[
                Span(
                    heading_path=[],
                    body="install Switchyard binary",
                    code_fence=False,
                    start_line=1,
                )
            ],
        ),
    ]

    async def fake_fanout(question, exclude_expr, top_k):
        assert question == "test question"
        assert exclude_expr is None
        assert top_k == 5
        return fake_excerpts

    monkeypatch.setattr(grounding, "_fanout_unscoped", fake_fanout)

    digest = asyncio.run(grounding.ground("test question", tag_expr=None, top_k=5))
    assert digest.no_coverage is False
    assert digest.no_library is False
    assert len(digest.citations) == 1
    assert digest.citations[0].slug == "switchyard/install.md"


def test_ground_unscoped_no_library_returns_no_library_true(monkeypatch) -> None:
    """When no library collections are registered, ground() returns
    no_library=True and no_coverage=True with empty citations."""
    from lies.mcp import grounding
    from lies.query import synthesizer as synth_mod

    monkeypatch.setattr(synth_mod, "_all_collection_names", lambda: [])

    digest = asyncio.run(grounding.ground("any question", tag_expr=None))
    assert digest.no_library is True
    assert digest.no_coverage is True
    assert digest.citations == []
    assert digest.searched_scope == []


def test_fanout_unscoped_threads_module_timeout_and_drops_failures(monkeypatch) -> None:
    """Per-call timeout uses the module-level ``_QMD_FANOUT_TIMEOUT``
    constant (default 15s; ``LIES_QMD_FANOUT_TIMEOUT`` env override)
    and ``QmdCommandError`` drops timed-out collections silently.

    Pins the post-#106 timeout constant: ``qmd_query`` is invoked
    with the module-level binding's current value (not the 60s
    default) so cold-daemon reranking has headroom. The constant is
    asserted ``isinstance(int) >= 10`` so a regression to a too-
    tight literal (the historical 5s bug) trips immediately.

    1. ``qmd_query`` is invoked with ``timeout=grounding._QMD_FANOUT_TIMEOUT``.
       The two-collection assertion pins the new value end-to-end.
    2. ``QmdCommandError`` — the error ``qmd_query`` raises on
       ``subprocess.TimeoutExpired`` — is caught by ``_one`` and the
       collection is dropped (returns ``None``).
    3. The fan-out still returns the surviving collections' hits.

    A real ``subprocess.run(timeout=N)`` would fire at N seconds in
    production; the mock emulates the observable contract
    (QmdCommandError) without burning the test budget on a sleep.
    The fail-soft envelope is what the budget needs to guard.
    Sequential dispatch is independently covered by
    ``test_fanout_unscoped_dispatches_sequentially`` below.
    """
    from lies.library import registry as reg_mod
    from lies.library.registry import LibraryCollectionMeta
    from lies.mcp import grounding
    from lies.qmd import cli as qmd_mod
    from lies.qmd.cli import QmdCommandError

    metas = [
        LibraryCollectionMeta(name="slow", tags=()),
        LibraryCollectionMeta(name="fast", tags=()),
    ]
    monkeypatch.setattr(reg_mod, "library_collection_metas", lambda: iter(metas))
    monkeypatch.setattr(reg_mod, "library_git_root", lambda: Path("/tmp/fake-lib"))

    observed_timeouts: list[int] = []

    def fake_qmd_query(*, cwd, question, limit, timeout, collection_filter):
        observed_timeouts.append(timeout)
        name = next(iter(collection_filter))
        if name == "slow":
            # Same shape ``qmd_query`` raises on
            # ``subprocess.TimeoutExpired`` — see ``src/lies/qmd/cli.py``.
            raise QmdCommandError(f"qmd query timed out after {timeout}s")
        return [
            {
                "path": f"{name}/page.md",
                "title": "Page",
                "score": 1.0,
            }
        ]

    monkeypatch.setattr(qmd_mod, "qmd_query", fake_qmd_query)

    excerpts = asyncio.run(grounding._fanout_unscoped("test question", None, top_k=5))

    # Timeout constant shape (must be ``int >= 10``): cold-daemon
    # reranking needs ~7s plus tail margin (matches the live-corpus
    # probe in features/2026-09-25-fanout-timeout/README.md). A
    # regression to ``5`` or a string-coerced value trips here.
    assert isinstance(grounding._QMD_FANOUT_TIMEOUT, int), (
        f"_QMD_FANOUT_TIMEOUT must be int, got {type(grounding._QMD_FANOUT_TIMEOUT).__name__}"
    )
    assert grounding._QMD_FANOUT_TIMEOUT >= 10, (
        f"_QMD_FANOUT_TIMEOUT={grounding._QMD_FANOUT_TIMEOUT} is below "
        f"the 10s cold-daemon reranking floor — qmd needs ~7s on "
        f"limit=10 plus tail margin"
    )
    # The fix: ``timeout=grounding._QMD_FANOUT_TIMEOUT`` threaded
    # through (not the 60s qmd_query default). Two calls because two
    # collections are registered.
    assert observed_timeouts == [
        grounding._QMD_FANOUT_TIMEOUT,
        grounding._QMD_FANOUT_TIMEOUT,
    ]
    # Slow collection's QmdCommandError caught and dropped; fast
    # collection's hit survives.
    assert len(excerpts) == 1
    assert excerpts[0].slug == "fast/page.md"


def test_fanout_unscoped_triggers_recycle_after_n_failures(monkeypatch) -> None:
    """``N`` consecutive ``QmdCommandError`` results trigger ``recycle()`` once.

    Pins the Task 5 fan-out recycle hook: when every dispatched
    ``_one`` raises ``QmdCommandError`` and the consecutive-error
    counter crosses ``_RECYCLE_THRESHOLD``, the fan-out fires
    ``recycle()`` exactly once before returning the (empty) excerpt
    list. Threshold is monkeypatched down to 2 so the test fires
    fast; the recycle mock clears the counter so a second batch of
    fan-outs would not see it double-count.
    """
    from lies.library import registry as reg_mod
    from lies.library.registry import LibraryCollectionMeta
    from lies.mcp import grounding
    from lies.qmd import cli as qmd_mod

    metas = [
        LibraryCollectionMeta(name="c0", tags=()),
        LibraryCollectionMeta(name="c1", tags=()),
        LibraryCollectionMeta(name="c2", tags=()),
    ]
    monkeypatch.setattr(reg_mod, "library_collection_metas", lambda: iter(metas))
    monkeypatch.setattr(reg_mod, "library_git_root", lambda: Path("/tmp/fake-lib"))

    monkeypatch.setattr(grounding, "_RECYCLE_THRESHOLD", 2)

    recycle_calls: list[str] = []

    def fake_recycle(*args, **kwargs):
        recycle_calls.append("called")
        # Mirror the real recycle: clear the counter so subsequent
        # fan-outs start fresh. Without this the counter is permanent
        # residue from the mock and every future test trips it.
        grounding._consecutive_qmd_errors = 0

    monkeypatch.setattr("lies.qmd.lifecycle.recycle", fake_recycle)

    def fake_qmd_query(*, cwd, question, limit, timeout, collection_filter):
        raise qmd_mod.QmdCommandError(f"simulated qmd failure for {next(iter(collection_filter))}")

    monkeypatch.setattr(qmd_mod, "qmd_query", fake_qmd_query)

    excerpts = asyncio.run(grounding._fanout_unscoped("any question", None, top_k=5))

    assert excerpts == []
    assert len(recycle_calls) == 1, (
        f"recycle called {len(recycle_calls)} times; expected 1 — "
        f"the consecutive-failure threshold tripped and one recycle "
        f"should fire per wedged-daemon batch."
    )


def test_fanout_unscoped_qmd_no_results_is_clean_miss(monkeypatch) -> None:
    """`QmdNoResultsError` is a clean miss — counter stays at 0, no recycle.

    Pins the post-PR #106 except-split: a per-collection
    ``QmdNoResultsError`` (qmd ran cleanly and returned zero hits for
    the question) must NOT increment ``_consecutive_qmd_errors``
    and must NOT trip ``recycle()``. The pre-fix tuple-caught both
    ``QmdCommandError`` AND ``QmdNoResultsError``, so the
    recycle counter tripped on legitimate empty results, eventually
    recycling a healthy daemon. The split promoted clean misses to
    a silent drop.

    Surface observed live: ``pydantic_validation`` legitimately
    returns ``QmdNoResultsError`` at ``top_k=10`` (the collection
    has limited reranking candidates). Pre-fix, this incremented
    the counter on every query; after enough calls the operator's
    healthy daemon was recycled.
    """
    from lies.library import registry as reg_mod
    from lies.library.registry import LibraryCollectionMeta
    from lies.mcp import grounding
    from lies.qmd import cli as qmd_mod

    metas = [
        LibraryCollectionMeta(name="c0", tags=()),
        LibraryCollectionMeta(name="c1", tags=()),
        LibraryCollectionMeta(name="c2", tags=()),
    ]
    monkeypatch.setattr(reg_mod, "library_collection_metas", lambda: iter(metas))
    monkeypatch.setattr(reg_mod, "library_git_root", lambda: Path("/tmp/fake-lib"))

    # Threshold below the failure count to demonstrate the bug if
    # the split regresses: with all 3 collections raising
    # ``QmdNoResultsError`` and threshold=2, the pre-fix tuple
    # incremented on every raise and would fire ``recycle()`` once
    # (counter goes 1 → 2 → reset → 1, threshold=2 trips once).
    monkeypatch.setattr(grounding, "_RECYCLE_THRESHOLD", 2)

    recycle_calls: list[str] = []

    def fake_recycle(*args, **kwargs):
        recycle_calls.append("called")
        # Mirror the real recycle: clear the counter so a subsequent
        # fan-out starts fresh (defense-in-depth — the split should
        # never let the counter trip in the first place).
        grounding._consecutive_qmd_errors = 0

    monkeypatch.setattr("lies.qmd.lifecycle.recycle", fake_recycle)

    def fake_qmd_query(*, cwd, question, limit, timeout, collection_filter):
        # Clean miss — qmd ran, found nothing for this query against
        # this collection's corpus. NOT a subprocess failure; the
        # post-split branch treats it as a silent drop with no
        # counter side-effect.
        raise qmd_mod.QmdNoResultsError(
            f"simulated qmd no-results for {next(iter(collection_filter))}"
        )

    monkeypatch.setattr(qmd_mod, "qmd_query", fake_qmd_query)

    excerpts = asyncio.run(grounding._fanout_unscoped("any question", None, top_k=5))

    # Load-bearing assertion #1: no hits (every collection was a
    # clean miss), but the digest shape is empty NOT a recycled
    # daemon side-effect.
    assert excerpts == []
    # Load-bearing assertion #2: ``recycle()`` was NOT called.
    # Pre-fix: tuple-caught ``QmdCommandError, QmdNoResultsError``
    # and the counter tripped on every raise — recycle fires when
    # counter crosses _RECYCLE_THRESHOLD=2. Post-fix: ``QmdNoResultsError``
    # is silently dropped without touching the counter, so the
    # recycle threshold never trips.
    assert recycle_calls == [], (
        f"recycle called {len(recycle_calls)} times on clean misses; "
        f"expected 0 — QmdNoResultsError must be a clean miss with "
        f"no counter side-effect (post-PR #106 except split)."
    )
    # Load-bearing assertion #3: the counter stayed at 0
    # end-to-end. The post-fix split leaves ``QmdNoResultsError``
    # out of the increment branch, so no failure of any kind
    # touches the counter on a clean-miss fan-out.
    assert grounding._consecutive_qmd_errors == 0, (
        f"_consecutive_qmd_errors={grounding._consecutive_qmd_errors} "
        f"after clean-miss fan-out; expected 0 — QmdNoResultsError "
        f"must not increment the recycle counter."
    )


def test_fanout_unscoped_resets_counter_after_success(monkeypatch) -> None:
    """A success between failures prevents the recycle trigger.

    Pins the success-reset branch of the recycle hook: with three
    collections and a threshold of 3, a (fail, success, fail) pattern
    keeps the counter below the threshold (it goes 1 → 0 → 1) and
    ``recycle()`` is never called. The success is keyed on the
    collection name rather than call count so the test is robust to
    the executor's thread-scheduling order.
    """
    from lies.library import registry as reg_mod
    from lies.library.registry import LibraryCollectionMeta
    from lies.mcp import grounding
    from lies.qmd import cli as qmd_mod

    metas = [
        LibraryCollectionMeta(name="c0", tags=()),
        LibraryCollectionMeta(name="c1", tags=()),
        LibraryCollectionMeta(name="c2", tags=()),
    ]
    monkeypatch.setattr(reg_mod, "library_collection_metas", lambda: iter(metas))
    monkeypatch.setattr(reg_mod, "library_git_root", lambda: Path("/tmp/fake-lib"))

    monkeypatch.setattr(grounding, "_RECYCLE_THRESHOLD", 3)

    recycle_calls: list[str] = []

    def fake_recycle(*args, **kwargs):
        recycle_calls.append("called")
        grounding._consecutive_qmd_errors = 0

    monkeypatch.setattr("lies.qmd.lifecycle.recycle", fake_recycle)

    def fake_qmd_query(*, cwd, question, limit, timeout, collection_filter):
        name = next(iter(collection_filter))
        if name == "c1":
            # Mid-fan-out success: clears the counter so the third
            # collection's failure restarts at 1, not at 2.
            return [
                {
                    "path": "c1/page.md",
                    "title": "Page",
                    "score": 0.9,
                    "snippet": "",
                }
            ]
        raise qmd_mod.QmdCommandError(f"simulated qmd failure for {name}")

    monkeypatch.setattr(qmd_mod, "qmd_query", fake_qmd_query)

    excerpts = asyncio.run(grounding._fanout_unscoped("any question", None, top_k=5))

    # The successful collection's excerpt survives; the two failures
    # are dropped. The point of the assertion is the empty recycle list.
    surviving_slugs = sorted(e.slug for e in excerpts)
    assert surviving_slugs == ["c1/page.md"]
    assert recycle_calls == [], (
        f"recycle called {len(recycle_calls)} times; expected 0 — "
        f"the success between failures must reset the counter before "
        f"the threshold trips."
    )


def test_fanout_unscoped_dispatches_sequentially(monkeypatch) -> None:
    """``_fanout_unscoped`` fires one qmd subprocess at a time.

    Pins the VRAM-spike fix: per-collection ``qmd_query`` calls must
    run strictly serially, not in parallel. Each concurrent
    ``qmd_query`` subprocess independently loads the embedding model
    into VRAM (live corpus reproduction: ``+c:opencode|c:claude_code``
    spiked VRAM when two subprocesses fired at once). The dispatch
    contract is one subprocess at a time, full stop.

    The test uses an event-list recording fake: every ``qmd_query``
    call appends ``(name, "start")`` on entry and ``(name, "end")``
    on exit. A 5ms ``time.sleep`` inside the fake ensures the
    start/end timestamps are deterministic across machines. The
    load-bearing assertion is the strict ``[start, end, start, end,
    start, end]`` interleaving — under the pre-fix ``asyncio.gather``
    dispatch, the three calls would interleave as
    ``[start, start, start, end, end, end]``.
    """
    import threading
    import time

    from lies.library import registry as reg_mod
    from lies.library.registry import LibraryCollectionMeta
    from lies.mcp import grounding
    from lies.qmd import cli as qmd_mod

    n = 3
    # 20ms per call keeps the wall-clock at ~60ms (3 × 20ms) for
    # sequential dispatch — well within the 0.15s unit-test hard
    # limit. Parallel dispatch would overlap all three calls at
    # ~20ms wall time; the strict interleaving assertion below
    # distinguishes the two regimes.
    hold_s = 0.02
    metas = [LibraryCollectionMeta(name=f"c{i}", tags=()) for i in range(n)]
    monkeypatch.setattr(reg_mod, "library_collection_metas", lambda: iter(metas))
    monkeypatch.setattr(reg_mod, "library_git_root", lambda: Path("/tmp/fake-lib"))

    events: list[tuple[str, str]] = []
    events_lock = threading.Lock()

    def fake_qmd_query(*, cwd, question, limit, timeout, collection_filter):
        name = next(iter(collection_filter))
        # ``asyncio.to_thread`` schedules each call on the default
        # executor's worker pool; appends are GIL-atomic in CPython,
        # but lock explicitly so the test reads as obviously correct.
        with events_lock:
            events.append((name, "start"))
        time.sleep(hold_s)
        with events_lock:
            events.append((name, "end"))
        return [{"path": f"{name}/page.md", "title": "Page", "score": 1.0}]

    monkeypatch.setattr(qmd_mod, "qmd_query", fake_qmd_query)

    excerpts = asyncio.run(grounding._fanout_unscoped("test question", None, top_k=5))

    # All N collections returned (none failed).
    assert len(excerpts) == n
    # Strict serial interleaving: each call's "start" follows the
    # previous call's "end". Under parallel ``asyncio.gather`` the
    # events would interleave as ``[start, start, start, end, end,
    # end]`` because all three workers fire before any of them
    # finishes. The dispatch contract is one subprocess at a time.
    assert len(events) == 2 * n, (
        f"events has {len(events)} entries; expected {2 * n} — "
        f"either a call did not record its end event or the dispatch "
        f"recorded extra starts"
    )
    for i in range(n):
        assert events[2 * i] == (f"c{i}", "start"), (
            f"event[{2 * i}] = {events[2 * i]}; expected (c{i}, start) — "
            f"dispatch fired out of order or with overlap"
        )
        assert events[2 * i + 1] == (f"c{i}", "end"), (
            f"event[{2 * i + 1}] = {events[2 * i + 1]}; expected (c{i}, end) — "
            f"dispatch interleaved or overlapped (parallel regression)"
        )
