"""Pin the reindex prompt routes through mcp__lies__reindex with flags."""

from __future__ import annotations

from tests.unit.mcp._prompt_body import rendered_body


def test_reindex_prompt_default_routes_update_only() -> None:
    from lies.mcp.prompts_impl import reindex_prompt

    [msg] = reindex_prompt("")
    body = rendered_body(msg)
    assert "mcp__lies__reindex" in body
    assert "reconcile=False" in body
    assert "embed=False" in body


def test_reindex_prompt_with_destructive_flag_warns_elicit() -> None:
    from lies.mcp.prompts_impl import reindex_prompt

    [msg] = reindex_prompt("--all")
    body = rendered_body(msg)
    assert "elicit" in body.lower() or "confirm" in body.lower()
    assert "all_=True" in body


def test_reindex_prompt_with_embed_routes_embed_flag() -> None:
    from lies.mcp.prompts_impl import reindex_prompt

    [msg] = reindex_prompt("--embed --force")
    body = rendered_body(msg)
    assert "embed=True" in body
    assert "force=True" in body


def test_reindex_accepts_all_three_all_spellings() -> None:
    """``all`` and ``all_`` are the positional spellings the pre-single-tail
    signature used, and ``/lies-reindex all_`` is the invocation that
    actually worked. Treating the bare positional as a no-op silently
    downgraded a destructive full rebuild to an ordinary one and dropped
    the confirmation warning with it."""
    from lies.mcp.prompts_impl import reindex_prompt

    for tail in ("all", "all_", "ALL", "--all", "All_"):
        [msg] = reindex_prompt(tail)
        body = rendered_body(msg)
        assert "all_=True" in body, tail
        assert "elicit-confirmation" in body, tail


def test_reindex_default_is_not_destructive() -> None:
    from lies.mcp.prompts_impl import reindex_prompt

    [msg] = reindex_prompt("")
    body = rendered_body(msg)
    assert "all_=False" in body
    assert "cleanup=False" in body
    assert "elicit" not in body.lower()


def test_reindex_threads_a_name_value() -> None:
    from lies.mcp.prompts_impl import reindex_prompt

    [msg] = reindex_prompt("--name mywiki --embed")
    body = rendered_body(msg)
    assert "name='mywiki'" in body
    assert "embed=True" in body


def test_reindex_names_an_unrecognized_flag() -> None:
    from lies.mcp.prompts_impl import reindex_prompt

    [msg] = reindex_prompt("--cleaup")
    body = rendered_body(msg)
    assert "Unrecognized flag(s) ignored: --cleaup." in body
    assert "cleanup=False" in body
