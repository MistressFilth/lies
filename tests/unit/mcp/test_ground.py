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


def test_fanout_unscoped_threads_5s_timeout_and_drops_failures(monkeypatch) -> None:
    """Concern #3 fix: thread ``timeout=5`` into ``qmd_query`` and drop
    timed-out collections silently.

    Implementer's note on the prior commit: the docstring promised a
    per-collection 5s timeout but the call site relied on
    ``qmd_query``'s default 60s — a single stuck collection would blow
    the whole 15s budget. After the fix:

    1. ``qmd_query`` is invoked with ``timeout=5`` (not the 60s
       default). The two-collection assertion pins the new value
       end-to-end.
    2. ``QmdCommandError`` — the error ``qmd_query`` raises on
       ``subprocess.TimeoutExpired`` — is caught by ``_one`` and the
       collection is dropped (returns ``None``).
    3. The fan-out still returns the surviving collections' hits.

    A real ``subprocess.run(timeout=5)`` would fire at 5s in
    production; the mock emulates the observable contract
    (QmdCommandError) without burning the test budget on a 5s sleep.
    The fail-soft envelope is what the budget needs to guard.
    Concurrency is independently covered by
    ``test_fanout_unscoped_runs_concurrently`` below.
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

    # The fix: ``timeout=5`` threaded through (not the 60s default).
    # Two calls because two collections are registered.
    assert observed_timeouts == [5, 5]
    # Slow collection's QmdCommandError caught and dropped; fast
    # collection's hit survives.
    assert len(excerpts) == 1
    assert excerpts[0].slug == "fast/page.md"


def test_fanout_unscoped_runs_concurrently(monkeypatch) -> None:
    """``asyncio.gather`` fans out qmd_query calls in parallel.

    Pins the concurrency property of ``_fanout_unscoped``: with N
    collections each holding the fan-out for ``hold_s`` seconds, the
    total wall time must be ≈ ``hold_s`` (concurrent), not N×
    ``hold_s`` (serial). Uses a short hold (50ms) so the test stays
    under the 0.15s unit-test hard limit.
    """
    import time

    from lies.library import registry as reg_mod
    from lies.library.registry import LibraryCollectionMeta
    from lies.mcp import grounding
    from lies.qmd import cli as qmd_mod

    n = 4
    hold_s = 0.05
    metas = [LibraryCollectionMeta(name=f"c{i}", tags=()) for i in range(n)]
    monkeypatch.setattr(reg_mod, "library_collection_metas", lambda: iter(metas))
    monkeypatch.setattr(reg_mod, "library_git_root", lambda: Path("/tmp/fake-lib"))

    def fake_qmd_query(*, cwd, question, limit, timeout, collection_filter):
        time.sleep(hold_s)
        name = next(iter(collection_filter))
        return [{"path": f"{name}/page.md", "title": "Page", "score": 1.0}]

    monkeypatch.setattr(qmd_mod, "qmd_query", fake_qmd_query)

    start = time.monotonic()
    excerpts = asyncio.run(grounding._fanout_unscoped("test question", None, top_k=5))
    elapsed = time.monotonic() - start

    # All N collections returned (none failed).
    assert len(excerpts) == n
    # Concurrent: total wall time ≈ hold_s. Serial would be N × hold_s
    # = 4 × 50ms = 200ms. Allow a generous ceiling (3 × hold_s) to
    # absorb CI scheduler jitter without giving up the load-bearing
    # concurrent-vs-serial distinction.
    assert elapsed < hold_s * 3, (
        f"fan-out took {elapsed:.3f}s with hold={hold_s}s; "
        f"serial would be ~{n * hold_s:.3f}s — asyncio.gather regression"
    )


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
