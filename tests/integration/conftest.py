"""Shared fixtures + integration gate for tests/integration/.

Every test in this directory skips unless ``INTEGRATION=1`` is set in the
environment. This is the single gate; individual files no longer need to
sprinkle their own ``pytestmark = pytest.mark.skipif(...)`` decorators.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

INTEGRATION_ENABLED = os.environ.get("INTEGRATION") == "1"
INTEGRATION_ROOT = Path(__file__).resolve().parent
CURATED_CORPUS_SRC = Path(__file__).resolve().parent.parent / "fixtures" / "library"


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every item collected under ``tests/integration/`` unless opted in.

    The hook fires for the whole session, so path-filter before tagging —
    without this guard the marker would land on unit tests too.
    """
    if INTEGRATION_ENABLED:
        return
    skip = pytest.mark.skip(reason="integration tests gated on INTEGRATION=1")
    for item in items:
        if item.path.is_relative_to(INTEGRATION_ROOT):
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _seed_librarian_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seed ``LIES_LIBRARIAN_MODEL=test`` for every integration test.

    The MCP ``ground`` tool calls :func:`lies.mcp.server._resolve_librarian_model`
    eagerly before dispatch, which raises
    :class:`lies.errors.ModelNotConfigured` when neither ``providers.toml``
    nor ``LIES_LIBRARIAN_MODEL`` is configured (the resolution surface is
    :func:`lies.providers.env_override`, not the agent-name-prefixed alias
    quoted in the error message). The CI sandbox has neither. Integration
    tests stub the librarian agent at the ``grounding.librarian_agent``
    import seam (mirrored for the scoped fast-path as
    ``_query_tagged_collections``), so a benign env override is sufficient:
    it short-circuits the ``providers.toml`` resolution without
    instantiating an Anthropic client.

    Tests that genuinely exercise a real model set ``LIES_LIBRARIAN_MODEL``
    themselves (not currently a case under ``tests/integration/``); the
    autouse does not interfere because ``monkeypatch.setenv`` is per-test.
    """
    monkeypatch.setenv("LIES_LIBRARIAN_MODEL", "test")


@pytest.fixture
def child_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Snapshot the XDG-redirected env so subprocesses see the same wiki.

    The autouse ``_isolated_xdg`` fixture in ``tests/conftest.py`` already
    redirected the five XDG base directories into ``tmp_path``; this
    fixture captures those redirections in a form we can hand to
    ``subprocess.run(env=...)`` so a child Python process resolves the
    same wiki as the parent.
    """
    keys = [k for k in os.environ if k.startswith(("XDG_", "LIES_XDG_"))]
    return {k: os.environ[k] for k in keys}


# ---------------------------------------------------------------------------
# curated_corpus — point LIES at the 5-collection fixture corpus
# ---------------------------------------------------------------------------
#
# Why function-scoped (not session-scoped as the brief suggests):
#   The brief asks for ``scope="session"`` autouse in ``tests/conftest.py``
#   pointing ``LIES_XDG_DATA_HOME`` at the corpus. pytest's
#   ``monkeypatch`` is function-scoped, so a session fixture cannot
#   guarantee its env-var setting survives ``_isolated_xdg``'s
#   ``monkeypatch.delenv("LIES_XDG_DATA_HOME", raising=False)`` for
#   every test in the session. Per-test monkeypatch isolates the
#   corpus copy and ensures the override is in place for the duration
#   of the test.
#
# Why tests/integration/conftest.py (not tests/conftest.py as the brief
# suggests):
#   ``tests/integration/conftest.py`` loads AFTER ``tests/conftest.py``,
#   so its fixtures run AFTER ``_isolated_xdg`` for any integration
#   test. This lets us override ``XDG_DATA_HOME`` (which
#   ``_isolated_xdg`` sets to ``tmp_path/xdg/data``) with the corpus
#   path. Putting the fixture in the root conftest would force every
#   unit test to copy the corpus for nothing.
#
# Why opt-in (not autouse as the brief suggests):
#   The brief says autouse. An autouse corpus fixture overrides
#   ``XDG_DATA_HOME`` for every integration test, which breaks
#   tests that spawn ``CliRunner`` sub-invocations expecting the
#   XDG-redirected tmp_path layout (e.g.
#   ``tests/integration/test_xdg_layout.py::test_init_then_query_then_lint_uses_xdg``,
#   which fails because the CLI's ``lies init e2e`` writes under
#   the corpus XDG instead of the redirected one). Tests opt in
#   by adding ``curated_corpus`` to their parameter list or by
#   decorating with ``@pytest.mark.usefixtures("curated_corpus")``.
#
# Why no qmd registration:
#   The tests stub the librarian + qmd seams (``librarian_agent_run``,
#   ``_post_query``, ``_qmd_get``) for determinism — no real qmd
#   round-trips, no BM25 variance. The corpus on disk is the source of
#   canned excerpt slugs the stubbed librarian returns; making the
#   collections visible via the registry is enough to keep the wire
#   contract honest (e.g., ``c:alpha`` resolves against
#   ``library_collection_names()``).


@pytest.fixture
def curated_corpus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Point LIES at the curated 5-collection corpus for integration tests.

    Copies ``tests/fixtures/library`` to ``<tmp>/lies/library/`` so the
    library path resolution
    (``xdg.data_home()/LIES_DATA_SUBDIR/library/collections/``) lands
    on the corpus copy. Overrides ``XDG_DATA_HOME`` to point at the
    copy, undoing ``_isolated_xdg``'s empty-XDG redirect for the
    duration of the test.

    Tests opt in by adding ``curated_corpus`` to their parameter list
    (or decorating with ``@pytest.mark.usefixtures("curated_corpus")``).
    The fixture yields the corpus root so individual tests can inspect
    the on-disk layout if they need to.
    """
    if not CURATED_CORPUS_SRC.exists():
        pytest.skip(f"curated corpus fixture missing at {CURATED_CORPUS_SRC}")

    dst = tmp_path / "lies_curated_corpus"
    dst.mkdir(parents=True, exist_ok=True)
    # ``data_home()/LIES_DATA_SUBDIR/library/`` must resolve to the
    # directory containing the ``collections/`` subdir, so we copy the
    # corpus contents under ``<dst>/lies/library/``.
    shutil.copytree(CURATED_CORPUS_SRC, dst / "lies" / "library", dirs_exist_ok=True)

    # Override ``_isolated_xdg``'s ``XDG_DATA_HOME`` so the library path
    # resolves through the corpus copy. ``_isolated_xdg`` also set
    # ``XDG_CONFIG_HOME`` etc. — those stay intact because we only
    # touch ``XDG_DATA_HOME`` here. The seeded providers.toml at
    # ``XDG_CONFIG_HOME/lies/providers.toml`` continues to load for
    # ``_resolve_synthesizer_model``.
    monkeypatch.setenv("XDG_DATA_HOME", str(dst))

    # Clear the library singletons so the next access resolves against
    # the redirected path. ``_isolated_xdg`` also calls
    # ``Library.open.cache_clear()``, but doing it here is a safety
    # net for the (rare) case where another fixture resolved the
    # library before ours ran.
    from lies.library.paths import Library

    Library.open.cache_clear()

    yield dst
