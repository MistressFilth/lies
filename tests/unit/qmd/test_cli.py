"""Tests for ``qmd_query`` in :mod:`lies.qmd.cli`.

The previous ``subprocess.run(capture_output=True, text=True, ...)``
shape was deadlock-prone on long stderr writes. The new
``_run_qmd`` helper in :mod:`lies.qmd._subprocess` replaces that
with Popen + ``communicate(timeout=...)`` and a DEVNULL stdin,
plus an 8 KB stderr truncation cap. The first test exercises the
integration so a future regression in ``qmd_query``'s subprocess
plumbing is caught at the call site.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from lies.qmd.cli import QmdError, qmd_query


@pytest.mark.slow
def test_qmd_query_does_not_deadlock_on_long_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """qmd_query must not deadlock when qmd emits a long stderr trace.

    Mocks the qmd binary so we can spawn a Python script that
    emits 100 KB to stderr then sleeps. The new ``_run_qmd`` path
    uses Popen + ``communicate(timeout=...)`` which drains both
    pipes concurrently and bounds stderr to 8 KB; the test
    asserts the call returns within 3 s.

    Regression guard only. ``subprocess.run(capture_output=...)``
    in modern Python (3.6+) uses threads to drain pipes and does
    not deadlock on a 100 KB stderr write either, so this passes
    against the old path; its value is catching a future change
    in ``qmd_query`` that drops the drain.
    """
    fake_qmd = tmp_path / "qmd"
    fake_qmd.write_text(
        "#!/bin/sh\n"
        f"{sys.executable} -c \"import sys; sys.stderr.write('x' * 100000); "
        'sys.stderr.flush(); import time; time.sleep(0.05)"\n'
    )
    fake_qmd.chmod(0o755)

    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ.get('PATH', '')}")

    t0 = time.monotonic()
    try:
        qmd_query(
            cwd=tmp_path,
            question="any",
            limit=5,
            timeout=5.0,
        )
    except QmdError:
        # The fake qmd exits cleanly with no stdout, so qmd_query
        # raises QmdNoResultsError (not QmdCommandError). Any
        # QmdError subclass is acceptable — what matters is that
        # the call returns promptly.
        pass
    dt = time.monotonic() - t0
    assert dt < 3.0, f"qmd_query took {dt:.2f}s; expected < 3s"


def test_parse_json_list_accepts_a_clean_payload() -> None:
    from lies.qmd.cli import _parse_json_list

    assert _parse_json_list('[{"path": "a/b.md"}]') == [{"path": "a/b.md"}]


def test_parse_json_list_tolerates_a_download_progress_prefix() -> None:
    """The real qmd shape: ipull's spinner, then the JSON.

    qmd's only stdout noise is ``ipull``'s progress while the model
    cache is cold (it writes through ``stdout-update`` with no TTY
    guard; LIES always pipes).
    """
    from lies.qmd.cli import _parse_json_list

    prefix = (
        "\x1b[?25l⠋ Gathering information\n"
        "\x1b[2K\x1b[1A\x1b[2K\x1b[G⠙ Gathering information\n"
        "\x1b[2K\x1b[1A\x1b[2K\x1b[G"
    )
    assert _parse_json_list(prefix + '[{"path": "a/b.md"}]') == [{"path": "a/b.md"}]


def test_parse_json_list_rejects_output_that_is_not_json() -> None:
    """A genuinely malformed response must still be rejected."""
    from lies.qmd.cli import _parse_json_list

    assert _parse_json_list("not json at all") is None
    assert _parse_json_list('[{"path": "truncated"') is None
    assert _parse_json_list("") is None


def test_parse_json_list_rejects_a_json_object_where_a_list_is_required() -> None:
    """qmd's contract is a list; an object means something else broke."""
    from lies.qmd.cli import _parse_json_list

    assert _parse_json_list('{"error": "boom"}') is None


def test_parse_json_list_keeps_scanning_past_a_json_object() -> None:
    """A JSON object before the list must not be reported as "no list"."""
    from lies.qmd.cli import _parse_json_list

    assert _parse_json_list('{"meta": 1}\n[{"path": "a/b.md"}]') == [{"path": "a/b.md"}]


def test_parse_json_list_handles_several_json_values_in_one_stream() -> None:
    """Later lists are still found after earlier non-list values."""
    from lies.qmd.cli import _parse_json_list

    assert _parse_json_list('{"a": 1}{"b": 2}\n[{"path": "x.md"}]') == [{"path": "x.md"}]


def test_parse_json_list_rejects_a_stream_of_only_objects() -> None:
    """Objects all the way through is still "no list", not an empty result."""
    from lies.qmd.cli import _parse_json_list

    assert _parse_json_list('{"a": 1}\n{"b": 2}') is None


def test_parse_json_list_does_not_requalify_the_whole_stream_per_bracket() -> None:
    """Forward parsing, not re-scan: a noisy prefix must not regress to O(n*m)."""
    from lies.qmd.cli import _parse_json_list

    noisy = "[" * 500 + "x" * 500
    assert _parse_json_list(noisy + ' [{"path": "a.md"}]') == [{"path": "a.md"}]


def test_qmd_query_enforces_the_envelope_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """``qmd_query(limit=N)`` returns at most N rows, even if the CLI ignores ``--limit``.

    The qmd CLI's option table at ``dist/cli/qmd.js:2550`` reads
    only ``values.n`` (the long option ``--limit`` is not parsed),
    and the call at ``:2428`` passes ``limit: results.length``,
    overriding the value outright. The LIES-side slice at the
    bottom of ``qmd_query`` is the load-bearing half; the daemon
    path forwards ``limit`` to a backend that honours it, but
    the CLI path does not, and the slice is duplicated on every
    path because every caller depends on it.
    """
    import json
    from types import SimpleNamespace

    many_rows = [{"path": f"claude_code/p{i}.md", "title": f"P{i}"} for i in range(20)]
    seen: dict[str, object] = {}

    def _fake_qmd(args, *, cwd, timeout, **kwargs):  # noqa: ARG001
        seen["args"] = list(args)
        return SimpleNamespace(
            args=tuple(args),
            returncode=0,
            stdout=json.dumps(many_rows).encode(),
            stderr=b"",
        )

    monkeypatch.setattr("lies.qmd.cli._run_qmd", _fake_qmd)
    monkeypatch.setattr("lies.library.registry.library_git_root", lambda: Path("/tmp/fake"))

    out = qmd_query(cwd=Path("/tmp/fake"), question="anything", limit=5, timeout=10)

    assert len(out) == 5, f"limit=5 must slice a 20-row response; got {len(out)}"
    args = seen["args"]
    assert args[-3:-1] == ["--limit", "5"], args


def test_a_scoped_query_filters_before_it_slices(monkeypatch: pytest.MonkeyPatch) -> None:
    """A collection filter must narrow the result set *before* the top-N slice.

    With every in-scope row ranked below the slice, ``filtered``
    comes back empty and ``qmd_query`` raises
    ``QmdNoResultsError`` — a claim that the corpus has no hits,
    for a query whose hits are sitting at rank 6. The librarian
    contract tells the model that flag means the corpus is empty
    for this question.
    """
    import json
    from types import SimpleNamespace

    rows = [{"path": f"claude_code/p{i}.md", "title": f"P{i}"} for i in range(20)]
    # Both mermaid rows rank *below* a limit=5 slice, so slicing
    # first loses every one of them.
    rows.append({"path": "mermaid/flowchart.md", "title": "Flowchart"})
    rows.append({"path": "mermaid/sequence.md", "title": "Sequence"})

    def _fake_qmd(args, *, cwd, timeout, **kwargs):  # noqa: ARG001
        return SimpleNamespace(
            args=tuple(args),
            returncode=0,
            stdout=json.dumps(rows).encode(),
            stderr=b"",
        )

    monkeypatch.setattr("lies.qmd.cli._run_qmd", _fake_qmd)
    monkeypatch.setattr("lies.library.registry.library_git_root", lambda: Path("/tmp/fake"))

    out = qmd_query(
        cwd=Path("/tmp/fake"),
        question="anything",
        limit=5,
        collection_filter={"mermaid"},
        timeout=10,
    )

    assert len(out) == 2, (
        f"a scoped query must return the in-scope rows, not the in-scope "
        f"subset of a global top-{5}; got {len(out)} of 2"
    )
    assert {hit["path"] for hit in out} == {"mermaid/flowchart.md", "mermaid/sequence.md"}


def test_a_scoped_query_still_honours_the_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fix must not turn ``limit`` into a no-op whenever a filter is present."""
    import json
    from types import SimpleNamespace

    rows = [{"path": f"mermaid/m{i}.md", "title": f"M{i}"} for i in range(10)]
    rows += [{"path": f"claude_code/p{i}.md", "title": f"P{i}"} for i in range(10)]

    def _fake_qmd(args, *, cwd, timeout, **kwargs):  # noqa: ARG001
        return SimpleNamespace(
            args=tuple(args),
            returncode=0,
            stdout=json.dumps(rows).encode(),
            stderr=b"",
        )

    monkeypatch.setattr("lies.qmd.cli._run_qmd", _fake_qmd)
    monkeypatch.setattr("lies.library.registry.library_git_root", lambda: Path("/tmp/fake"))

    out = qmd_query(
        cwd=Path("/tmp/fake"),
        question="anything",
        limit=3,
        collection_filter={"mermaid"},
        timeout=10,
    )

    assert len(out) == 3, f"limit=3 must still apply to a scoped query; got {len(out)}"


def test_an_embed_that_aborts_on_the_cuda_reservation_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The intermittent VMM-reservation abort is absorbed, not surfaced.

    Measured on this host: 1-3 aborts per 32 embeds across three full
    runs of the tag-filter integration file, a different test failing
    each time, never more than one qmd subprocess running and never
    above 7.7 GB of 24.5 GB of VRAM. The reservation is inside
    node-llama-cpp and cannot be fixed from here; the flake can be.
    Embedding is idempotent and the abort writes nothing, so retrying
    is safe.
    """
    from types import SimpleNamespace

    from lies.qmd import cli

    calls: list[int] = []

    def fake_run(args, cwd, timeout, **kwargs):  # noqa: ARG001
        calls.append(1)
        if len(calls) < 3:  # abort twice, then succeed
            return SimpleNamespace(
                args=tuple(args),
                returncode=1,
                stdout=b"",
                stderr=(
                    b"ggml-cuda.cu:98: CUDA error: out of memory\n"
                    b"  cuMemAddressReserve(&pool_addr, CUDA_POOL_VMM_MAX_SIZE, 0, 0, 0)"
                ),
            )
        return SimpleNamespace(args=tuple(args), returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(cli, "_run", fake_run)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    cli.qmd_embed(Path("/tmp"), "coll", timeout=600)

    assert len(calls) == 3, f"expected two retries then success, got {len(calls)} calls"


def test_a_real_embed_failure_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """A genuine qmd error must still surface on the first attempt.

    The retry is deliberately narrow. Absorbing a real failure would
    triple the wall time of every genuine embed error and hide it.
    """
    from types import SimpleNamespace

    from lies.qmd import cli

    calls: list[int] = []

    def fake_run(args, cwd, timeout, **kwargs):  # noqa: ARG001
        calls.append(1)
        return SimpleNamespace(
            args=tuple(args),
            returncode=1,
            stdout=b"",
            stderr=b"qmd: no such collection: nope",
        )

    monkeypatch.setattr(cli, "_run", fake_run)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    with pytest.raises(cli.QmdError, match="no such collection"):
        cli.qmd_embed(Path("/tmp"), "coll", timeout=600)

    assert len(calls) == 1, f"a real failure was retried {len(calls)} times"


def test_an_embed_that_never_stops_aborting_eventually_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry is bounded: a persistent abort still surfaces."""
    from types import SimpleNamespace

    from lies.qmd import cli

    calls: list[int] = []

    def fake_run(args, cwd, timeout, **kwargs):  # noqa: ARG001
        calls.append(1)
        return SimpleNamespace(
            args=tuple(args),
            returncode=1,
            stdout=b"",
            stderr=b"cuMemAddressReserve: CUDA error: out of memory",
        )

    monkeypatch.setattr(cli, "_run", fake_run)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    with pytest.raises(cli.QmdError, match="cuMemAddressReserve"):
        cli.qmd_embed(Path("/tmp"), "coll", timeout=600)

    assert len(calls) == cli._EMBED_RESERVATION_RETRIES + 1
