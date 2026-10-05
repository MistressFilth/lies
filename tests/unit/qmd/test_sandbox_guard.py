"""A sandboxed library must not be able to write the live qmd index.

``qmd`` resolves its index from ``$XDG_CACHE_HOME``, not from
``$XDG_DATA_HOME``, and it is a single file keyed by collection name
that every process on the machine shares. Redirecting the *library*
root into a temp directory therefore sandboxes the mirror files and
nothing else: the collection rows still land in
``~/.cache/qmd/index.sqlite``.

Measured, not inferred::

    $ XDG_DATA_HOME=/tmp/sandbox-data qmd --help | grep ^Index:
    Index: /home/divinefilth/.cache/qmd/index.sqlite

    $ XDG_DATA_HOME=/tmp/sandbox-data XDG_CACHE_HOME=/tmp/sandbox-cache qmd --help | grep ^Index:
    Index: /tmp/sandbox-cache/qmd/index.sqlite

``AGENTS.local.md`` says to run ingest work "against a sandboxed XDG
root, never against this library", which reads as sufficient and is
not: it sandboxes ``XDG_DATA_HOME`` and leaves the index live. The
cost was measured too -- a sandboxed batch ingest registered its
collections in the operator's live index, and the cleanup afterwards
was manual.

The fix is not better advice, it is a refusal. Redirecting the data
root while leaving the cache root at its default is a combination no
caller wants: the library is throwaway and the index is not. So the
qmd write helpers refuse it and name both variables.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lies.qmd import cli

#: A real ``CompletedProcess``, because ``_run`` decodes ``stdout`` /
#: ``stderr`` and reads ``args`` off the result. A hand-rolled stub
#: with only ``returncode`` fails on ``.args`` for reasons that have
#: nothing to do with what these tests are about.
_HIT = b'[{"path": "alpha/p.md", "title": "P", "score": 0.9}]'


def _ok(args: list[str], stdout: bytes = b"") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr=b"")


def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, data: str | None, cache: str | None):
    """Set the two roots the qmd index decision depends on, cleanly."""
    for key in (
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "LIES_XDG_DATA_HOME",
        "LIES_XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(key, raising=False)
    if data is not None:
        monkeypatch.setenv("XDG_DATA_HOME", data)
    if cache is not None:
        monkeypatch.setenv("XDG_CACHE_HOME", cache)


def test_a_sandboxed_library_cannot_write_the_live_index(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The exact combination that leaked: data redirected, cache not.

    Both halves matter and neither alone is the bug. Redirecting the
    cache without the data is harmless -- the index is isolated and
    nothing is shared. Redirecting the data without the cache is the
    leak: throwaway mirror files, shared index rows.
    """
    _env(monkeypatch, tmp_path, data=str(tmp_path / "data"), cache=None)

    with pytest.raises(cli.QmdIndexIsolationError) as excinfo:
        cli._assert_qmd_index_isolated()

    message = str(excinfo.value)
    assert "XDG_DATA_HOME" in message, "the refusal must name the variable that was redirected"
    assert "XDG_CACHE_HOME" in message, "the refusal must name the variable that was not"


def test_redirecting_both_roots_is_allowed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The documented sandbox — all roots redirected — is the supported case."""
    _env(
        monkeypatch,
        tmp_path,
        data=str(tmp_path / "data"),
        cache=str(tmp_path / "cache"),
    )

    cli._assert_qmd_index_isolated()  # must not raise


def test_neither_root_redirected_is_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ordinary operator case: a real ingest into a real library."""
    _env(monkeypatch, tmp_path, data=None, cache=None)

    cli._assert_qmd_index_isolated()  # must not raise


def test_a_cache_redirect_alone_is_allowed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Redirecting only the cache isolates the index; the library is real.

    Not symmetric with the dangerous case, and deliberately so: this
    one shares nothing, so refusing it would break a legitimate way of
    running a second index against the real library.
    """
    _env(monkeypatch, tmp_path, data=None, cache=str(tmp_path / "cache"))

    cli._assert_qmd_index_isolated()  # must not raise


def test_the_lies_override_counts_as_redirected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``LIES_XDG_*`` is read first, so it must be read by the guard too.

    A guard that only inspected ``XDG_*`` would pass a caller who
    sandboxed through the LIES-prefixed variable -- which is the
    spelling this repository documents.
    """
    _env(monkeypatch, tmp_path, data=None, cache=None)
    monkeypatch.setenv("LIES_XDG_DATA_HOME", str(tmp_path / "data"))

    with pytest.raises(cli.QmdIndexIsolationError):
        cli._assert_qmd_index_isolated()

    monkeypatch.setenv("LIES_XDG_CACHE_HOME", str(tmp_path / "cache"))
    cli._assert_qmd_index_isolated()  # must not raise


def test_the_write_helpers_refuse_before_spawning_qmd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The guard runs before the subprocess, so nothing is written.

    Checking after would be worthless: the point is that the row is
    never created.
    """
    _env(monkeypatch, tmp_path, data=str(tmp_path / "data"), cache=None)
    spawned: list[list[str]] = []
    monkeypatch.setattr(
        "lies.qmd.cli._run_qmd",
        lambda args, **kw: spawned.append(list(args)),
    )

    with pytest.raises(cli.QmdIndexIsolationError):
        cli.qmd_update(cwd=tmp_path)

    assert spawned == [], f"a qmd process was spawned against the live index: {spawned!r}"


def test_the_spawn_recorder_actually_observes_a_permitted_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The recorder works.

    ``qmd_run_qmd`` is a module global in ``cli``, so patching it on
    ``_subprocess`` silently records nothing. Without this the refusal
    test above passes vacuously -- the subprocess would never have run
    for an unrelated reason, and the assertion would be about nothing.
    """
    _env(monkeypatch, tmp_path, data=None, cache=None)
    calls: list[list[str]] = []

    monkeypatch.setattr(
        "lies.qmd.cli._run_qmd",
        lambda args, **kw: (calls.append(list(args)), _ok(args))[1],
    )

    cli.qmd_update(cwd=tmp_path)

    assert calls == [["qmd", "update"]], f"the recorder must observe an allowed call; got {calls!r}"


def test_reads_are_not_guarded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A sandboxed library may still *read* the live index.

    The daemon is machine-global and serves the live index to every
    client on the machine; guarding reads would break the read path
    for the only configuration that is actually safe.
    """
    _env(monkeypatch, tmp_path, data=str(tmp_path / "data"), cache=None)
    calls: list[list[str]] = []

    monkeypatch.setattr(
        "lies.qmd.cli._run_qmd",
        # qmd_query raises QmdNoResultsError on an empty list, so the
        # stub has to carry a result or the test would fail for a
        # reason with nothing to do with the guard.
        lambda args, **kw: (calls.append(list(args)), _ok(args, _HIT))[1],
    )

    rows = cli.qmd_query(cwd=tmp_path, question="anything")

    assert calls, "the read must still have run"
    assert rows and rows[0]["path"] == "alpha/p.md"
