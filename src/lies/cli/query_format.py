"""``--format`` flag + render dispatch for ``lies query``.

Adds the ``--format=auto|md|table|marp|chart`` flag to the ``query`` command.
Default is ``auto`` (the synthesizer's format_hint is the rendered
format). Explicit values trigger the override path: if the
synthesizer's hint differs, re-synthesize with a constrained prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, cast

import typer

FormatLiteral = Literal["auto", "md", "table", "marp", "chart"]
_VALID_FORMATS = {"auto", "md", "table", "marp", "chart"}


def render_answer(
    answer_format: Literal["md", "table", "marp", "chart"],
    body: str,
    *,
    output_dir: Path | None = None,
) -> None:
    """Render the answer body via the format-specific renderer.

    ``answer_format`` is the validated format (already past the
    format_validator). The CLI prints the rendered output via stdout.
    """
    if answer_format == "marp":
        from lies.query.formats.marp import render_marp

        output_path = render_marp(body, output_dir=output_dir)
        typer.echo(str(output_path))
        if output_path.suffix == ".html":
            typer.echo("(rendered with marp CLI)")
        else:
            typer.echo(
                "(marp CLI not detected on PATH; rendered to markdown; "
                "run `marp <path>` to get HTML/PDF)"
            )
        return

    if answer_format == "table":
        from lies.query.formats.table import render_table

        typer.echo(render_table(body))
        return

    if answer_format == "chart":
        from lies.query.formats.chart import render_chart

        rendered = render_chart(body)
        if rendered != body:
            typer.echo(rendered)
            return
        # Pass-through: the synth emitted no mermaid block. Surface
        # the body unchanged and a stderr warning so the operator
        # can re-query. Matches the override-failure warning shape
        # at src/lies/cli/query.py:336-340.
        typer.echo(rendered)
        typer.echo(
            "warning: --format=chart produced no mermaid block; emitted body unchanged.",
            err=True,
        )
        return

    # md
    from lies.query.formats.md import render_markdown

    typer.echo(render_markdown(body))


def validate_format_flag(value: str) -> FormatLiteral:
    """Validate --format values; raise BadParameter for unknown."""
    if value not in _VALID_FORMATS:
        raise typer.BadParameter(
            f"--format must be one of {sorted(_VALID_FORMATS)}; got {value!r}",
            param_hint="--format",
        )
    # Cast through ``Literal`` — the set membership check above is the
    # runtime gate; the type checker doesn't narrow ``str`` to the
    # literal union just from a containment test.
    return cast(FormatLiteral, value)


__all__ = ("FormatLiteral", "render_answer", "validate_format_flag")
