"""``--format`` flag + render dispatch for ``lies query``.

Adds the ``--format=auto|md|table|marp|chart`` flag to the ``query`` command.
Default is ``auto`` (the synthesizer's format_hint is the rendered
format). Explicit values trigger the override path: if the
synthesizer's hint differs, re-synthesize with a constrained prompt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal, cast

import typer

FormatLiteral = Literal["auto", "md", "table", "marp", "chart"]
_VALID_FORMATS = frozenset({"auto", "md", "table", "marp", "chart"})


def _render_marp(body: str, output_dir: Path | None) -> None:
    """Marp dispatch: write the slide file, print the path + status note."""
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


def _render_table(body: str, _output_dir: Path | None) -> None:
    """Table dispatch: stdout the rendered table."""
    from lies.query.formats.table import render_table

    typer.echo(render_table(body))


def _render_md(body: str, _output_dir: Path | None) -> None:
    """Markdown dispatch: stdout the rendered markdown."""
    from lies.query.formats.md import render_markdown

    typer.echo(render_markdown(body))


def _render_chart(body: str, _output_dir: Path | None) -> None:
    """Chart dispatch: stdout the longest ```mermaid``` block.

    Pass-through policy (F1 chart addendum § Pass-through policy): zero
    mermaid blocks → body unchanged + stderr warning so the operator
    can re-query. Skips the body echo when ``body`` is empty to avoid
    printing a blank line before the warning.
    """
    from lies.query.formats.chart import render_chart

    rendered = render_chart(body)
    if rendered != body:
        typer.echo(rendered)
        return
    if body:
        typer.echo(rendered)
    typer.echo(
        "warning: --format=chart produced no mermaid block; emitted body unchanged.",
        err=True,
    )


# Per-format render dispatcher. Mirrors the spec's RENDERERS table
# (F1 chart addendum § CLI dispatch) — one entry per format, uniform
# ``(body, output_dir) -> None`` signature. ``"marp"`` is the
# side-effect outlier (writes a slide file); the wrapper
# ``_render_marp`` carries that behavior. Adding a format = add a key.
_RENDERERS: dict[
    Literal["md", "table", "marp", "chart"],
    Callable[[str, Path | None], None],
] = {
    "md": _render_md,
    "table": _render_table,
    "marp": _render_marp,
    "chart": _render_chart,
}


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
    _RENDERERS[answer_format](body, output_dir)


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
