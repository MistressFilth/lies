"""Top-level CLI package. Defines the root typer app and wires sub-apps.

Importing this module is cheap: heavy dependencies (orchestrator,
pydantic-ai, anthropic) are NOT imported here. They are imported in
the group modules' command bodies, and only when a command that
needs them runs.

A module-level ``__getattr__`` re-exports a small set of names that
tests (and possibly downstream tooling) reach through attribute
access on the package -- ``lies.cli.Orchestrator``,
``lies.cli.pick_scraper``, etc. Each access is forwarded to a lazy
import that loads the heavy module only when the attribute is
actually read. This preserves the "no orchestrator / no anthropic on
bare import" contract that ``tests/unit/cli/test_cli_lazy_imports.py``
pins.

The order of imports below is load-bearing:

1. The root ``app = typer.Typer(...)`` is defined first.
2. The sub-app objects (``mcp_app``, ``flock_app``, ``providers_app``,
   ``collections_app``) are imported and ``app.add_typer(...)`` is
   called for each. The sub-app modules run their decorator
   registrations at this point (each sub-app's decorators target its
   own sub-app instance, not the root ``app``).
3. The group modules that decorate ``app.command(...)`` directly
   (``_core``, ``ingestion``, ``query``) are imported last. They
   ``from lies.cli import app`` at module top -- this works because
   ``app`` was bound in step 1, and Python's import system returns
   the partially-loaded ``lies.cli`` namespace rather than re-executing
   ``__init__.py``.
"""

from __future__ import annotations

import typer

# Step 1: define the root typer app.
# ``no_args_is_help=False`` is intentional: with no subcommand, the
# callback's REPL runs. Setting ``no_args_is_help=True`` would suppress
# the REPL and dump help on bare ``lies``.
app = typer.Typer(
    name="lies",
    help="Library of Inconsistent Explanations & Sources -- a Karpathy-pattern LLM wiki.",
)

# Step 2: import the sub-app objects (cheap -- just typer.Typer instances)
# and wire them under the root app.
from lies.cli.collections import collections_app
from lies.cli.memory import memory_app
from lies.cli.operator import flock_app, mcp_app, providers_app

app.add_typer(mcp_app, name="mcp", rich_help_panel="Operator tooling")
app.add_typer(flock_app, name="flock", rich_help_panel="Operator tooling")
app.add_typer(providers_app, name="providers", rich_help_panel="Operator tooling")
app.add_typer(memory_app, name="memory", rich_help_panel="Querying and maintenance")
app.add_typer(collections_app, name="collections", rich_help_panel="Wiki management")


# The REPL callback -- invoked when ``lies`` is run with no subcommand.
# Lives in __init__.py so the bare ``lies`` command (no subcommand) is the REPL.
@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    name: str | None = typer.Option(
        None,
        "--name",
        envvar="LIES_WIKI_NAME",
        help="Wiki for REPL commands (default: $LIES_WIKI_NAME).",
    ),
    no_memory: bool = typer.Option(
        False,
        "--no-memory",
        help="Disable invisible wiki memory for free-form REPL commands.",
    ),
) -> None:
    """REPL mode when no subcommand is given."""
    if ctx.invoked_subcommand is not None:
        return
    from rich.console import Console
    from rich.markdown import Markdown

    from lies.cli import Orchestrator as _Orchestrator
    from lies.cli import resolve_wiki as _resolve_wiki
    from lies.cli._helpers import configure_logging
    from lies.wiki.git import atomic_commit

    configure_logging()
    wiki = _resolve_wiki(name)
    orch = _Orchestrator(wiki)
    console = Console()
    console.print("[bold]LIES REPL[/bold] -- type /help for commands, /exit to leave.")
    while True:
        try:
            line = console.input("lies> ")
        except EOFError, KeyboardInterrupt:
            break
        line = line.strip()
        if not line:
            continue
        if line in ("/exit", "/quit"):
            break
        if line == "/help":
            console.print(
                "Commands:\n"
                "  /ingest <source>   Add a source to the wiki\n"
                "  /query <question>  Ask a question\n"
                "  /lint              Health-check the wiki\n"
                "  /status            qmd status + last 10 log entries\n"
                "  /commit            Force a git commit\n"
                "  /exit              Leave the REPL"
            )
            continue
        if line == "/commit":
            try:
                sha = atomic_commit(wiki.data_root, "manual commit")
                if sha is None:
                    typer.echo("nothing to commit")
                else:
                    typer.echo(f"committed {sha[:8]}")
            except Exception as exc:  # noqa: BLE001 - commit failures must not crash the REPL
                typer.echo(f"commit failed: {exc}")
            continue
        # Otherwise, dispatch as a free-form command
        output = orch.run(line) if no_memory else orch.run_with_memory(line)
        console.print(Markdown(output))
    console.print("\nbye.")


# Step 3: import the group modules that register @app.command(...) decorators
# directly on the root ``app``. Each module's top-level code is cheap; only
# its command bodies are expensive.
# Importing these triggers decorator registration -- they need the root
# ``app`` to already exist (which it does from step 1), and Python's import
# machinery returns the partially-loaded ``lies.cli`` namespace rather than
# re-executing this __init__.py.
from lies.cli import _core, ingestion, operator, query  # noqa: F401

# Re-exports for test compat. ``test_cli_flock.py`` monkeypatches
# ``cli_module.acquire_create_lock``; ``test_cli_lint_force_repair.py``
# reaches ``WikiFlockUnrepairable`` / ``WikiLockBusy`` through the same
# ``import lies.cli as cli_module`` alias. These are cheap so they're
# re-exported directly.
from lies.cli._helpers import (
    WikiFlockUnrepairable,
    WikiLockBusy,
    acquire_create_lock,
)

# Lazy re-exports for test compat. Several tests do
# ``monkeypatch.setattr("lies.cli.Orchestrator", ...)`` /
# ``with patch("lies.cli.pick_scraper"): ...`` etc.; Python's attribute
# lookup walks the package, so the names must be reachable as attributes
# of ``lies.cli``. Loading them eagerly here would defeat the
# lazy-imports contract; module-level ``__getattr__`` loads each one on
# first access only.
_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    # attr name -> (module path, attribute name on that module)
    "Orchestrator": ("lies.orchestrator", "Orchestrator"),
    "pick_scraper": ("lies.scrapers.base", "pick_scraper"),
    "resolve_wiki": ("lies.mcp.resolution", "resolve_wiki"),
    "WikiLinkResolver": ("lies.wikilinks", "WikiLinkResolver"),
    "WikiLinkCorpusMissing": ("lies.wikilinks", "WikiLinkCorpusMissing"),
    "_stdout_isatty": ("lies.cli._helpers", "_stdout_isatty"),
}


def __getattr__(name: str):
    """Lazy re-export for test compatibility.

    Each access loads the heavy module on demand. ``import lies.cli``
    does not trigger these -- only attribute access does.
    """
    spec = _LAZY_ATTRS.get(name)
    if spec is None:
        raise AttributeError(f"module 'lies.cli' has no attribute {name!r}")
    import importlib

    module_path, attr_name = spec
    module = importlib.import_module(module_path)
    value = getattr(module, attr_name)
    # Cache on the package so subsequent accesses are a normal attribute
    # read -- keeps ``hasattr`` honest and avoids re-importing each call.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    """Include the lazy attrs in dir(lies.cli) for tab-completion and IDE help."""
    return sorted(set(globals().keys()) | set(_LAZY_ATTRS.keys()))


__all__ = (
    "WikiFlockUnrepairable",
    "WikiLockBusy",
    "acquire_create_lock",
    "app",
    "collections_app",
    "flock_app",
    "mcp_app",
    "providers_app",
)
