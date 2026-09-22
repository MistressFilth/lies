"""Unit tests: all 8 LIES MCP prompts register on the FastMCP instance.

Pre-existing prompt (`ask_wiki_answer`, registered as `answer`) plus
the 7 reference-prose prompts (`orient`, `ingest`, `query`, `lint`,
`sync`, `file-back`, `cite`). Reads the FastMCP internal prompt
registry; if the API surface changes in a future FastMCP version,
the assertion message points the implementer at the new attribute
name.
"""

from __future__ import annotations

from lies.mcp.server import mcp


EXPECTED_PROMPTS = {
    "answer",
    "orient",
    "ingest",
    "query",
    "lint",
    "sync",
    "file-back",
    "cite",
}


def _registered_prompt_names() -> set[str]:
    """Recover the set of registered prompt names.

    FastMCP 4.x stores prompts on an internal registry. Try the
    documented attribute first, fall back to walking the
    `_prompts` dict if necessary.
    """
    pm = getattr(mcp, "_prompt_manager", None)
    if pm is not None:
        names = getattr(pm, "_prompts", None)
        if names:
            return set(names.keys())
        listed = getattr(pm, "list_prompts", None)
        if listed is not None:
            return {p.name for p in listed()}
    # Final fallback: introspect server._prompts
    raw = getattr(mcp, "_prompts", None)
    if raw:
        return set(raw.keys())
    # FastMCP 4.0.x actually stores components under
    # ``_local_provider._components`` keyed by ``prompt:<name>@``.
    provider = getattr(mcp, "_local_provider", None)
    if provider is not None:
        comps = getattr(provider, "_components", None)
        if comps:
            names: set[str] = set()
            for key, val in comps.items():
                if key.startswith("prompt:"):
                    name = getattr(val, "name", None) or key.split(":", 1)[1].rstrip("@")
                    if name:
                        names.add(name)
            if names:
                return names
    raise AssertionError(
        "Could not recover prompt names from FastMCP instance; "
        "update _registered_prompt_names() for the current FastMCP API."
    )


def test_all_eight_prompts_registered() -> None:
    registered = _registered_prompt_names()
    missing = EXPECTED_PROMPTS - registered
    assert not missing, f"missing prompt registrations: {sorted(missing)}"
