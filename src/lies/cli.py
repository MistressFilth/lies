"""Typer CLI entrypoint."""
from __future__ import annotations

import typer

from lies import __version__
from lies.config import get_model, get_wiki_root

app = typer.Typer(
    name="lies",
    help="Library of Inconsistent Explanations & Sources — a Karpathy-pattern LLM wiki.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the LIES version and exit."""
    typer.echo(f"lies {__version__}")


@app.command()
def config() -> None:
    """Print the current LIES configuration."""
    typer.echo(f"model: {get_model()}")
    typer.echo(f"wiki_root: {get_wiki_root()}")


if __name__ == "__main__":
    app()