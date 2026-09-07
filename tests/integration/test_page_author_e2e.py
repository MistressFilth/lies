"""End-to-end integration test for ``lies page write`` (F39 + F12).

Gated on ``INTEGRATION=1``. Requires ``qmd`` binary on ``PATH`` (it's a
runtime dependency of :class:`WikiMemoryService.apply_plan` — the
``qmd update`` reindex step is non-fatal, but the test still exercises
the real subprocess path).

The test drives the public ``lies`` CLI through three real
``subprocess.run`` invocations against a freshly-initialized wiki in
the test-scoped ``tmp_path``:

1. First ``lies page write`` – creates the page; stdout carries
   ``create: <rel-path>``.
2. Second ``lies page write`` (same path, no ``--force``) – collision;
   CLI exits ``2`` with the documented ``page already exists`` stderr.
3. Third ``lies page write`` with ``--force`` – overwrite; stdout
   carries ``update: <rel-path>``.

The ``_isolated_xdg`` autouse fixture in ``tests/conftest.py`` already
redirects every XDG root to ``tmp_path/xdg/<role>/`` and clears
``LIES_WIKI_NAME``, so the subprocess inherits an isolated XDG tree via
``os.environ.copy()`` and a clean wiki name we set explicitly.

Without ``INTEGRATION=1``, the module skips cleanly so the unit suite
does not depend on a live ``qmd`` install or on a writable tmp XDG
tree.

Deviation from brief's literal test code
----------------------------------------

The brief's literal snippet passes ``--name test-integration`` to
``lies init``, but the actual ``init`` subcommand declares ``name`` as
a positional ``typer.Argument`` (``src/lies/cli/_core.py:134``), so
``--name`` is rejected by typer with ``No such option: --name``. The
test below invokes ``lies init test-integration`` positionally —
semantically identical to the brief's intent; the ``lies page write``
calls keep ``--name`` because ``name`` *is* an ``Option`` on
``page write`` (``src/lies/cli/page.py:135``).

Current test status
-------------------

This test currently FAILS when run with ``INTEGRATION=1`` because the
``lies page write`` CLI does not pre-register plan evidence with the
``WikiMemoryService`` before invoking ``apply_plan``. The plan builder
emits ``evidence=[f"{collection}/{slug}"]`` (e.g. ``["claude-code/hooks"]``)
which ``validate_operation_evidence`` checks against
``WikiMemoryService._known_evidence``. The F3 ``run_query`` flow works
because it pre-registers evidence (``orchestrator.py:1539``); the
``Orchestrator.file_back_synthesis`` and ``Orchestrator.file_back_author``
wrappers do not register anything themselves, so the CLI/MCP surface
that bypasses ``run_query`` hits ``WikiEvidenceMissing``. The CLI exits
0 (errors-as-values), but stdout carries
``(author: error — file_back_crashed: WikiEvidenceMissing: …)`` rather
than the expected ``(author: durably filed ... - create: ...)``.

The default CI run (without ``INTEGRATION=1``) still skips cleanly, so
this failing integration test does not break CI — but it documents a
real implementation gap in Tasks 8 and 11 that the unit suite (which
mocks ``file_back_author``) never caught. The fix belongs in either
``src/lies/cli/page.py:write`` and ``src/lies/mcp/server.py:file_knowledge``
(register evidence before calling ``file_back_author``) or in
``Orchestrator.file_back_author`` itself (register the plan's evidence
before delegating to ``apply_plan``).
"""

from __future__ import annotations

import os
import subprocess

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("INTEGRATION"),
    reason="set INTEGRATION=1 to run",
)


def test_page_write_then_overwrite(tmp_path):
    """First write -> create; second write without --force -> fail; with --force -> update."""
    env = os.environ.copy()
    env["LIES_WIKI_NAME"] = "test-integration"
    # The page-write path constructs the Orchestrator (which builds
    # every sub-agent's provider), so the subprocess needs *some*
    # ANTHROPIC_API_KEY to instantiate the AnthropicProvider even
    # though no agent call is ever made. A dummy value is enough;
    # pydantic-ai will only complain if a real call hits the wire.
    env.setdefault("ANTHROPIC_API_KEY", "test-integration-dummy-key-not-used")

    # Init wiki + collection
    subprocess.run(["uv", "run", "lies", "init", "test-integration"], check=True, env=env)

    body = "## Body\nFirst version."

    # First write
    result = subprocess.run(
        [
            "uv",
            "run",
            "lies",
            "page",
            "write",
            "--collection",
            "claude-code",
            "--type",
            "concept",
            "--slug",
            "hooks",
            "--title",
            "Hooks",
            "--body-file",
            "-",
            "--name",
            "test-integration",
        ],
        input=body,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"first write failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "create:" in result.stdout

    # Second write without --force
    result = subprocess.run(
        [
            "uv",
            "run",
            "lies",
            "page",
            "write",
            "--collection",
            "claude-code",
            "--type",
            "concept",
            "--slug",
            "hooks",
            "--title",
            "Hooks",
            "--body-file",
            "-",
            "--name",
            "test-integration",
        ],
        input=body,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 2, (
        f"second write should exit 2 on collision: "
        f"stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    # Third write with --force
    result = subprocess.run(
        [
            "uv",
            "run",
            "lies",
            "page",
            "write",
            "--collection",
            "claude-code",
            "--type",
            "concept",
            "--slug",
            "hooks",
            "--title",
            "Hooks",
            "--body-file",
            "-",
            "--force",
            "--name",
            "test-integration",
        ],
        input=body,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, (
        f"third write (--force) failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "update:" in result.stdout
