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
``recycle``) are loaded on first use rather than at import.
``import lies.qmd.lifecycle`` triggers loading of :mod:`lies.qmd`'s
package ``__init__``, which eagerly imports :class:`QmdCapability`
from :mod:`lies.qmd.capability` -- that import pulls in
:mod:`fastmcp` and :mod:`pydantic_ai`, both of which are heavy
startup costs that must NOT ride the ``import lies.cli`` chain
(``tests/unit/cli/test_cli_lazy_imports`` pins this contract).

The binding goes through :func:`_lifecycle`, which resolves a name
into this module's ``globals`` on first use. It did not used to work:
the command bodies dereferenced the bare name (``status``) on the
theory that a PEP 562 module ``__getattr__`` would catch it. A module
``__getattr__`` fires on *attribute* access (``qmd_cli.status``),
never on the bare-global lookup a function body performs -- that goes
``globals()`` then ``builtins``. Every one of these four commands
raised ``NameError: name 'status' is not defined`` on the first real
invocation, and the ``# noqa: F821 -- resolved via __getattr__``
comment asserted the opposite. Nothing caught it: the module had no
test, and a test that patched ``qmd_cli.status`` would have masked it
by writing the name into ``globals`` first.
"""

from __future__ import annotations

import json

import typer

app = typer.Typer(help="qmd daemon lifecycle", no_args_is_help=True)


# Mirror of ``lies.qmd.lifecycle._DEFAULT_PORT`` so the @app.command()
# option defaults can reference a constant without importing the
# heavy lifecycle module at CLI startup. Update both sides if the
# default ever changes.
_DEFAULT_PORT = 8181


_LAZY_LIFECYCLE_ATTRS: tuple[str, ...] = ("status", "_up", "_down", "recycle")


def __getattr__(name: str):
    """Resolve ``status`` / ``_up`` / ``_down`` / ``recycle`` on first access.

    PEP 562 module-level ``__getattr__`` — Python calls it when
    ``qmd_cli.<name>`` is looked up and the name is not in the
    module's ``globals()``. It caches the resolved symbol in the
    globals so later lookups are a plain attribute read.

    It does **not** fire for the bare name a function body in this
    module would resolve; :func:`_lifecycle` is what the command
    bodies call, and it is the only thing that makes them work.
    """
    if name in _LAZY_LIFECYCLE_ATTRS:
        from lies import qmd as _qmd_pkg  # noqa: PLC0415 - PEP 562 lazy import

        _qmd_pkg  # touch: ensures ``import lies.cli`` does not pre-load this
        from lies.qmd import lifecycle as _lifecycle_mod  # noqa: PLC0415 - PEP 562 lazy import

        value = getattr(_lifecycle_mod, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _lifecycle(name: str):
    """The lazy lifecycle primitive ``name``, resolved on first use.

    The command bodies call this rather than dereferencing a bare
    name. A module ``__getattr__`` only sees attribute access, so
    ``globals()[name]`` inside a function body would never find it —
    that is the ``NameError`` this helper exists to prevent.

    A cached name short-circuits, so a
    ``monkeypatch.setattr(qmd_cli, "status", …)`` that runs after the
    first call is still what later calls see.
    """
    if name not in globals():
        __getattr__(name)
    return globals()[name]


@app.command(name="status")
def status_cmd(
    port: int = typer.Option(_DEFAULT_PORT, "--port", "-p"),
) -> None:
    """Print daemon status as JSON, plus an integrity snapshot.

    The integrity surface is read-only against qmd's index
    (``$XDG_CACHE_HOME/qmd/index.sqlite``); see
    :mod:`lies.qmd.integrity`. A missing index is reported as
    ``null`` so a stale or freshly-spawned daemon does not raise.

    The four existing fields (``running``, ``pid``, ``port``,
    ``url``) are unchanged. The ``index`` block is additive.
    """
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

    A clean exit means a present, openable index. A missing index
    (the daemon was just spawned and ``lies sync`` has not run, or
    the cache root was wiped) reports ``null`` — daemon state is
    still meaningful, and an empty index is not an error.
    """
    import sqlite3

    from lies.qmd.integrity import integrity_summary, qmd_index_path

    db = qmd_index_path()
    if not db.exists():
        return None
    try:
        return integrity_summary(db)
    except sqlite3.OperationalError:
        # The file exists but is not a SQLite database (corrupt,
        # mid-write, foreign). Daemon state is still meaningful;
        # the integrity block degrades to ``null`` rather than failing
        # the operator command.
        return None


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
