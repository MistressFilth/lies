"""``lies qmd <cmd>`` must resolve its lazily-bound lifecycle primitives.

Every command in ``src/lies/cli/qmd.py`` dereferenced the primitive as
a bare name (``status``, ``_up``, …) on the theory that the module's
PEP 562 ``__getattr__`` would catch it. A module ``__getattr__`` fires
on *attribute* access (``qmd_cli.status``), never on the bare-global
lookup a function body performs, which goes ``globals()`` then
``builtins``. All four commands raised

    NameError: name 'status' is not defined

on their first real invocation. The ``# noqa: F821`` comment on each
line asserted the opposite, so the linter stayed quiet and the bug
shipped.

Nothing caught it because the module had no test. These call the
command functions with a stubbed lifecycle so no daemon is touched —
the point is that the *name resolves at all*, which is what was
broken.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from lies.cli import qmd as qmd_cli


@dataclass
class _Snapshot:
    running: bool = True
    pid: int = 4242
    port: int = 8181
    url: str = "http://127.0.0.1:8181"


def test_status_command_resolves_the_lifecycle_primitive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The regression: this raised NameError before the fix.

    ``monkeypatch.setattr`` is what makes it pass, and that is the
    point — the patched name lands in the module's ``globals``, so a
    test written this way would have masked the production bug. The
    assertion that matters is that the command body got far enough to
    call the primitive at all.
    """
    monkeypatch.setattr(qmd_cli, "status", lambda port: _Snapshot(port=port))
    qmd_cli.status_cmd(port=9999)
    payload = json.loads(capsys.readouterr().out)
    assert payload["running"] is True
    assert payload["pid"] == 4242
    assert payload["port"] == 9999


@pytest.mark.parametrize(
    ("command", "attr", "kwargs"),
    [
        ("up", "_up", {"port": 8181}),
        ("down", "_down", {"port": 8181}),
        ("recycle_cmd", "recycle", {"port": 8181, "ready_timeout": 0.0}),
    ],
)
def test_every_qmd_command_resolves_its_primitive(
    command: str,
    attr: str,
    kwargs: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(qmd_cli, attr, lambda **kw: _Snapshot())
    getattr(qmd_cli, command)(**kwargs)
    assert capsys.readouterr().out.strip()


def test_a_command_resolves_with_nothing_cached_in_globals(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The regression, unmasked.

    Every other test here patches the primitive first, and
    ``monkeypatch.setattr(qmd_cli, "status", …)`` writes the name into
    the module's ``globals`` — so a bare ``status(...)`` in the
    command body would find it there and pass. That is precisely how
    the bug survived review: any test written the obvious way makes
    the broken code work.

    Clearing the slot first removes the crutch. The command then has
    to resolve the name itself, which is the thing that was broken.
    ``status`` is a read-only reachability probe, so calling the real
    one touches nothing.
    """
    monkeypatch.delitem(qmd_cli.__dict__, "status", raising=False)
    qmd_cli.status_cmd(port=qmd_cli._DEFAULT_PORT)
    assert json.loads(capsys.readouterr().out)["port"] == qmd_cli._DEFAULT_PORT


def test_lifecycle_helper_caches_so_a_later_patch_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A monkeypatch after the first resolution is what later calls see.

    The cached slot in ``globals`` is the documented seam tests use, so
    a first call must not close it.
    """
    qmd_cli._lifecycle("status")
    sentinel = object()
    monkeypatch.setattr(qmd_cli, "status", sentinel)
    assert qmd_cli._lifecycle("status") is sentinel
