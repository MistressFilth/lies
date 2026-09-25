"""``lies qmd`` -- operator subcommands for the shared qmd daemon.

Mirrors ask's ``scripts/qmd-daemon.py`` operator surface so
operators fluent in ask's daemon management can apply the same
commands to LIES. Subcommands:

- ``status`` -- print daemon snapshot as JSON.
- ``up`` -- start the daemon (idempotent; safe to re-run).
- ``down`` -- stop the daemon (best-effort).
- ``recycle`` -- restart the daemon and wait for liveness.

The actual subprocess work lives in :mod:`lies.qmd.lifecycle`; this
module is a thin typer wrapper that forwards CLI flags and prints
operator-facing messages. No subprocess is spawned here.

Lazy ``lies.qmd.lifecycle`` binding
-----------------------------------

The lifecycle primitives (``status``, ``_up``, ``_down``,
``recycle``) are loaded via a PEP 562 module-level ``__getattr__``
on first attribute access. ``import lies.qmd.lifecycle`` triggers
loading of :mod:`lies.qmd`'s package ``__init__``, which eagerly
imports :class:`QmdCapability` from
:mod:`lies.qmd.capability` -- that import pulls in :mod:`fastmcp`
and :mod:`pydantic_ai`, both of which are heavy startup costs that
must NOT ride the ``import lies.cli`` chain
(``tests/unit/cli/test_cli_lazy_imports`` pins this contract).

Each command body dereferences the bare name (``status``, ``_up``,
etc.) so the module-level lookup fires the lazy loader on first
command invocation, not at CLI import. Test patches use
``monkeypatch.setattr(qmd_cli, "status", ...)`` -- the lazy
``__getattr__`` caches the resolved value in the module's globals
on first access, and a subsequent ``monkeypatch.setattr`` writes
over that cached slot so the patched value is what subsequent
function bodies see.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import typer

if TYPE_CHECKING:
    # Imported only for type-checker visibility; the runtime import
    # is deferred through the ``__getattr__`` shim below.
    from lies.qmd.lifecycle import _down, _up, recycle, status

app = typer.Typer(help="qmd daemon lifecycle", no_args_is_help=True)


# Mirror of ``lies.qmd.lifecycle._DEFAULT_PORT`` so the @app.command()
# option defaults can reference a constant without importing the
# heavy lifecycle module at CLI startup. Update both sides if the
# default ever changes.
_DEFAULT_PORT = 8181


_LAZY_LIFECYCLE_ATTRS: tuple[str, ...] = ("status", "_up", "_down", "recycle")


def __getattr__(name: str):
    """Resolve ``status`` / ``_up`` / ``_down`` / ``recycle`` on first access.

    PEP 562 module-level ``__getattr__`` — Python calls it when a
    bare name (``status``, ``_up``, …) is looked up at module scope
    and not present in the module's ``globals()``. The first access
    imports :mod:`lies.qmd.lifecycle` (which transitively loads the
    heavy :mod:`lies.qmd` package) and caches the resolved symbol
    in the module globals so subsequent lookups are a normal
    attribute read. Tests that patch
    ``monkeypatch.setattr(qmd_cli, "_up", …)`` after first access
    overwrite the cached slot — the patched value is what the
    command bodies see on their next access.
    """
    if name in _LAZY_LIFECYCLE_ATTRS:
        from lies import qmd as _qmd_pkg  # noqa: PLC0415 - PEP 562 lazy import

        _qmd_pkg  # touch: ensures ``import lies.cli`` does not pre-load this
        from lies.qmd import lifecycle as _lifecycle  # noqa: PLC0415 - PEP 562 lazy import

        value = getattr(_lifecycle, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@app.command(name="status")
def status_cmd(
    port: int = typer.Option(_DEFAULT_PORT, "--port", "-p"),
) -> None:
    """Print daemon status as JSON."""
    s = status(port=port)  # noqa: F821 — resolved via __getattr__
    typer.echo(
        json.dumps(
            {
                "running": s.running,
                "pid": s.pid,
                "port": s.port,
                "url": s.url,
            },
            indent=2,
        )
    )


@app.command()
def up(
    port: int = typer.Option(_DEFAULT_PORT, "--port", "-p"),
) -> None:
    """Start the daemon (idempotent)."""
    s = _up(port=port)  # noqa: F821 — resolved via __getattr__
    typer.echo(f"qmd daemon running (pid {s.pid}) at {s.url}")


@app.command()
def down(
    port: int = typer.Option(_DEFAULT_PORT, "--port", "-p"),
) -> None:
    """Stop the daemon (best-effort)."""
    _down(port=port)  # noqa: F821 — resolved via __getattr__
    typer.echo("qmd daemon stopped")


@app.command(name="recycle")
def recycle_cmd(
    port: int = typer.Option(_DEFAULT_PORT, "--port", "-p"),
    ready_timeout: float = typer.Option(30.0, "--ready-timeout"),
) -> None:
    """Restart the daemon; wait for liveness."""
    s = recycle(port=port, ready_timeout=ready_timeout)  # noqa: F821 — resolved via __getattr__
    typer.echo(f"qmd daemon recycled: pid {s.pid} at {s.url}")
