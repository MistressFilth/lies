"""``lies qmd`` -- operator subcommands for the shared qmd daemon.

Mirrors ask's ``scripts/qmd-daemon.py``. Subcommands: ``status``,
``up``, ``down``, ``recycle``. The actual subprocess work lives
in :mod:`lies.qmd.lifecycle`; this module is a thin typer
wrapper that forwards CLI flags and prints operator-facing
messages.

The lifecycle primitives are loaded on first use; ``import
lies.qmd.lifecycle`` eagerly imports :class:`QmdCapability`,
which pulls in :mod:`fastmcp` and :mod:`pydantic_ai`.
"""

from __future__ import annotations

import json

import typer

app = typer.Typer(help="qmd daemon lifecycle", no_args_is_help=True)


# Mirror of ``lies.qmd.lifecycle._DEFAULT_PORT`` so @app.command()
# option defaults can reference it without importing the heavy
# lifecycle module at CLI startup.
_DEFAULT_PORT = 8181


_LAZY_LIFECYCLE_ATTRS: tuple[str, ...] = ("status", "_up", "_down", "recycle")


def __getattr__(name: str):
    """Resolve lazy lifecycle attrs on first access (PEP 562).

    Caches the resolved symbol in ``globals``. :func:`_lifecycle`
    is what the command bodies call — a module ``__getattr__``
    does not fire for a bare name inside a function body.
    """
    if name in _LAZY_LIFECYCLE_ATTRS:
        from lies.qmd import lifecycle as _lifecycle_mod  # noqa: PLC0415 - PEP 562 lazy import

        globals()[name] = getattr(_lifecycle_mod, name)
        return globals()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _lifecycle(name: str):
    """Resolve the lazy lifecycle primitive ``name`` on first use.

    A module ``__getattr__`` only sees attribute access, so a
    bare name inside a function body would never find it.
    """
    if name not in globals():
        __getattr__(name)
    return globals()[name]


@app.command(name="status")
def status_cmd(
    port: int = typer.Option(_DEFAULT_PORT, "--port", "-p"),
) -> None:
    """Print daemon status as JSON, plus an integrity snapshot."""
    s = _lifecycle("status")(port=port)
    index_block = _integrity_block()
    typer.echo(
        json.dumps(
            {
                "running": s.running,
                "pid": s.pid,
                "port": s.port,
                "url": s.url,
                "index": index_block,
            },
            indent=2,
        )
    )


def _integrity_block() -> dict[str, object] | None:
    """The ``index`` block for the status command, or ``None``.

    ``None`` means qmd was never indexed here. A database that
    exists but cannot be read -- a WAL that needs recovery, so a
    ``mode=ro`` connection gets ``SQLITE_CANTOPEN`` while creating
    the ``-shm`` file -- reports ``{"error": ...}`` instead.
    Collapsing the two into ``null`` told the operator "no index"
    about an index that is right there and needs one command to open.
    """
    import sqlite3

    from lies.qmd.integrity import integrity_summary, qmd_index_path

    db = qmd_index_path()
    if not db.exists():
        return None
    try:
        return integrity_summary(db)
    except sqlite3.OperationalError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}


@app.command()
def up(
    port: int = typer.Option(_DEFAULT_PORT, "--port", "-p"),
) -> None:
    """Start the daemon (idempotent)."""
    s = _lifecycle("_up")(port=port)
    typer.echo(f"qmd daemon running (pid {s.pid}) at {s.url}")


@app.command()
def down(
    port: int = typer.Option(_DEFAULT_PORT, "--port", "-p"),
) -> None:
    """Stop the daemon (best-effort)."""
    _lifecycle("_down")(port=port)
    typer.echo("qmd daemon stopped")


@app.command(name="recycle")
def recycle_cmd(
    port: int = typer.Option(_DEFAULT_PORT, "--port", "-p"),
    ready_timeout: float = typer.Option(30.0, "--ready-timeout"),
) -> None:
    """Restart the daemon; wait for liveness."""
    s = _lifecycle("recycle")(port=port, ready_timeout=ready_timeout)
    typer.echo(f"qmd daemon recycled: pid {s.pid} at {s.url}")
