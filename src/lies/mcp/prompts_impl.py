"""Prompt implementations — module-level functions decorators wrap.

Each ``<name>_prompt`` function in this module returns a list of
``Message`` for the prompt it implements. ``register_all`` decorates
each one with ``@mcp.prompt(name=...)`` and binds it to the live
``mcp``. Tests drive the impl functions directly via
``prompts_impl.<name>_prompt(...)`` without spinning up an MCP
instance.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.prompts import Message  # noqa: F401  (re-exported for impl fns)

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_all(mcp: FastMCP) -> None:
    """Wire each impl function as ``@mcp.prompt`` on ``mcp``."""

    @mcp.prompt(
        name="ask",
        description="Synthesized cited answer to a question.",
    )
    def _ask_prompt(
        question: str,
        tag_expr: str | None = None,
        exclude_tags: list[str] | None = None,
    ) -> list[Message]:
        return ask_prompt(question, tag_expr, exclude_tags)

    @mcp.prompt(
        name="ground",
        description="Cite-snippet digest (no synthesis).",
    )
    def _ground_prompt(
        question: str,
        tag_expr: str | None = None,
        exclude_tags: list[str] | None = None,
        top_k: int = 3,
    ) -> list[Message]:
        return ground_prompt(question, tag_expr, exclude_tags, top_k)

    @mcp.prompt(
        name="collections",
        description="Library collection registry CRUD (list, add, remove, info, tag).",
    )
    def _collections_prompt(
        subcommand: str,
        args: list[str] | None = None,
    ) -> list[Message]:
        return collections_prompt(subcommand, args)

    @mcp.prompt(
        name="ingest",
        description="Bring a source into the library (single, batch, or delete).",
    )
    def _ingest_prompt(
        source: str,
        delete_slug: str | None = None,
        batch_dir: str | None = None,
        dry_run: bool = False,
    ) -> list[Message]:
        return ingest_prompt(source, delete_slug, batch_dir, dry_run)


# Concrete impl functions added by tasks 3-9 below.


def ask_prompt(
    question: str,
    tag_expr: str | None = None,
    exclude_tags: list[str] | None = None,
) -> list[Message]:
    """Synthesized cited answer to a question."""
    body = (
        f"Call mcp__lies__search({question!r}, tag_expr={tag_expr!r}, "
        f"exclude_tags={exclude_tags!r}) to find hits. "
        f"Read each top-ranked page body via mcp__lies__read([path]). "
        f"Then call mcp__lies__lib_ask({question!r}, "
        f"tag_expr={tag_expr!r}, exclude_tags={exclude_tags!r}) for "
        f"a synthesized cited answer. "
        f'Cite each claim as [[collection/slug]]: "verbatim quote from the cited span".'
    )
    return [Message(body)]


def ground_prompt(
    question: str,
    tag_expr: str | None = None,
    exclude_tags: list[str] | None = None,
    top_k: int = 3,
) -> list[Message]:
    """Cite-snippet digest (no synthesis)."""
    body = (
        f"Call mcp__lies__search({question!r}, tag_expr={tag_expr!r}, "
        f"exclude_tags={exclude_tags!r}) to find hits. "
        f"Read each top-ranked page body via mcp__lies__read([path]). "
        f"Render each citation as "
        f'[[collection/slug]] (Title): "≤200-char verbatim snippet" '
        f"(clamped to top_k={top_k} entries). "
        f"Cite marker is grounded in the read span's body, not synthesized prose. "
        f"Do NOT route through lib_ask — ground is digest-only."
    )
    return [Message(body)]


def collections_prompt(
    subcommand: str,
    args: list[str] | None = None,
) -> list[Message]:
    """Library collection registry CRUD."""
    args = list(args or [])
    sub = subcommand.strip().lower()

    if sub == "list":
        body = (
            'Call mcp__lies__collections_read(subcommand="list") and '
            "render each entry as one markdown bullet (name, tags, "
            "source, page_count, updated_at)."
        )
    elif sub == "info":
        name = args[0] if args else "<name>"
        body = (
            f'Call mcp__lies__collections_read(subcommand="info", '
            f"name={name!r}) and render the returned metadata envelope."
        )
    elif sub == "add":
        joined = " ".join(args)
        body = (
            f"Register a new collection: run "
            f"Bash(lies library new <name> <path> [--tags <t1,t2>] "
            f"[--scope <keywords>] [--synonyms <file>] [--scraper <cmd>]) "
            f"with args {joined!r}. Then run qmd embed so vec/hyde queries "
            f"find the new collection."
        )
    elif sub in ("remove", "modify"):
        joined = " ".join(args)
        body = (
            f"Run Bash(lies library {sub} {joined}) and report the tool's "
            f"outcome to the user verbatim."
        )
    elif sub == "tag":
        joined = " ".join(args)
        body = (
            f"Run Bash(lies library enrich-tags {joined}) and report which "
            f"collections now carry the tag."
        )
    elif sub in ("register-shipped", "where"):
        joined = " ".join(args)
        body = f"Run Bash(lies library {sub} {joined}) and surface the stdout stream to the user."
    else:
        body = (
            f"Unknown subcommand {sub!r}. Valid subcommands: "
            f"list, add, remove, modify, info, tag, register-shipped, where. "
            f"Ask the user which to invoke."
        )
    return [Message(body)]


def ingest_prompt(
    source: str,
    delete_slug: str | None = None,
    batch_dir: str | None = None,
    dry_run: bool = False,
) -> list[Message]:
    """Route a source into the library."""
    dry = " --dry-run" if dry_run else ""
    if delete_slug:
        cmd = f'lies ingest --data-dir "$LIES_DATA"{dry} --delete {delete_slug}'
        body = (
            f"Run Bash({cmd!r}) and surface stdout/stderr. The CLI "
            f"removes the page file, the catalog row, appends a "
            f"delete entry to the log, and triggers qmd update."
        )
    elif batch_dir:
        body = (
            f"Run Bash(lies ingest --batch {batch_dir!r}{dry} "
            f'--data-dir "$LIES_DATA" --slug-prefix <derive-from-user> '
            f"--force) — slug-prefix is required. Surface stdout/stderr."
        )
    else:
        body = (
            f'Run Bash(lies ingest --data-dir "$LIES_DATA"{dry} '
            f"--source {source!r} --type <entity|concept|synthesis|...> "
            f'[--slug <slug>] [--title "<title>"]) — supervised mode '
            f"requires --type. Surface stdout/stderr."
        )
    return [Message(body)]
