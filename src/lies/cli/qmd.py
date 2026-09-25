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
"""

from __future__ import annotations

import json

import typer

from lies.qmd.lifecycle import (
    _DEFAULT_PORT,
    _down,
    _up,
    recycle,
    status,
)

app = typer.Typer(help="qmd daemon lifecycle", no_args_is_help=True)


@app.command(name="status")
def status_cmd(
    port: int = typer.Option(_DEFAULT_PORT, "--port", "-p"),
) -> None:
    """Print daemon status as JSON."""
    s = status(port=port)
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
    s = _up(port=port)
    typer.echo(f"qmd daemon running (pid {s.pid}) at {s.url}")


@app.command()
def down(
    port: int = typer.Option(_DEFAULT_PORT, "--port", "-p"),
) -> None:
    """Stop the daemon (best-effort)."""
    _down(port=port)
    typer.echo("qmd daemon stopped")


@app.command(name="recycle")
def recycle_cmd(
    port: int = typer.Option(_DEFAULT_PORT, "--port", "-p"),
    ready_timeout: float = typer.Option(30.0, "--ready-timeout"),
) -> None:
    """Restart the daemon; wait for liveness."""
    s = recycle(port=port, ready_timeout=ready_timeout)
    typer.echo(f"qmd daemon recycled: pid {s.pid} at {s.url}")
