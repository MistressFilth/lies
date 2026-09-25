"""Shared pytest fixtures."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic_ai.models import Model

from lies.constants import LIES_DATA_SUBDIR
from lies.wiki.wiki import Wiki

FIXTURE_WIKI = Path(__file__).parent / "fixtures" / "sample-wiki"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the ``--runslow`` flag at the rootdir conftest.

    Pytest parses CLI args before conftest.py files deeper in the tree
    are loaded, so a ``--runslow`` flag registered in
    ``tests/unit/conftest.py`` is rejected with ``unrecognized
    arguments`` when pytest is invoked at the repo root with a path
    argument (e.g. ``pytest tests/ --runslow``). Registering here — the
    first conftest pytest loads — makes the flag visible at CLI parse
    time across every invocation shape (``pytest``, ``pytest
    tests/unit/``, ``pytest tests/``).
    """
    parser.addoption(
        "--runslow",
        action="store_true",
        default=False,
        help="run tests marked as slow (default: skip them)",
    )


def make_wiki(name: str, data_root: Path) -> Wiki:
    """Build a Wiki rooted at ``data_root`` with all five role roots set.

    The conftest autouse fixture redirects the five ``XDG_*`` env vars to
    ``tmp_path/xdg/<role>/``. This factory takes a caller-supplied
    ``data_root`` (usually ``tmp_path / "wiki"``) and computes the other
    four role roots from the corresponding XDG env vars so the resulting
    Wiki is internally consistent and matches what `lies init` would have
    produced.
    """
    import os

    # ``data_home`` is read so the env var participates in test isolation
    # (its absence would silently fall through to the real home dir); we
    # accept the explicit unused assignment for that sanity check.
    data_home = Path(os.environ["XDG_DATA_HOME"])  # noqa: F841
    config_home = Path(os.environ["XDG_CONFIG_HOME"])
    cache_home = Path(os.environ["XDG_CACHE_HOME"])
    state_home = Path(os.environ["XDG_STATE_HOME"])
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        runtime_root = Path(runtime_dir) / "lies" / name
    else:
        runtime_root = state_home / "run" / "lies" / name
    return Wiki(
        name=name,
        data_root=data_root,
        config_root=config_home / "lies" / name,
        cache_root=cache_home / "lies" / name,
        state_root=state_home / "lies" / name,
        runtime_root=runtime_root,
    )


def models_for_tests(value: Model | str) -> dict[str, Model | str]:
    """Build an ``Orchestrator(models=...)`` dict mapping every roster entry to ``value``.

    Tests that need a single model for every agent call this helper so
    the existing ``Orchestrator(wiki=wiki, model=X)`` test sites can be
    rewritten in one line as ``Orchestrator(wiki=wiki, models=models_for_tests(X))``.
    The orchestrator's per-agent ``Model | str`` parameter type accepts
    any value that pydantic-ai's ``Agent`` accepts, including ``TestModel()``.
    """
    from lies.providers import AGENT_ROSTER

    return {name: value for name in AGENT_ROSTER}


@pytest.fixture(autouse=True)
def _isolated_xdg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reset LIES / XDG env and redirect all XDG roots to tmp_path.

    Each test starts with a clean slate: every ``LIES_*``, ``LIES_XDG_*``,
    and ``XDG_*`` variable is removed, and the five XDG base directories
    are redirected to ``tmp_path/xdg/<role>/``. The wiki and pipeline
    code resolves its storage through these roots, so per-test isolation
    prevents tests from leaking state into the developer's real
    ``~/.local/share/lies`` etc.

    Also strips ``GIT_DIR`` from the environment. Pre-commit (and the
    bare-repo-with-worktrees layout) sets ``GIT_DIR`` to the parent
    worktree's gitdir when invoking ``git commit``; ``subprocess.run`` in
    tests inherits that, so ``git rev-parse --show-toplevel`` resolves
    to the worktree instead of the fresh tmp dir and breaks tests that
    assert the tmp dir is *not* a git repo. Clearing it here means the
    test subprocess sees the tmp dir's git state directly.
    """
    for key in [
        "LIES_WIKI_NAME",
        "LIES_WIKI_ROOT",
        "LIES_QMD_TRANSPORT",
        "LIES_QMD_URL",
        "LIES_LOG_LEVEL",
        "LIES_XDG_DATA_HOME",
        "LIES_XDG_CONFIG_HOME",
        "LIES_XDG_CACHE_HOME",
        "LIES_XDG_STATE_HOME",
        "LIES_XDG_RUNTIME_DIR",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_RUNTIME_DIR",
        "GIT_DIR",
    ]:
        monkeypatch.delenv(key, raising=False)
    # Library is a ``lru_cache(maxsize=1)`` singleton (Library.open). The
    # cache is built once against whichever XDG path was current at the
    # first call; every later call returns the cached Library even when
    # XDG has been repointed. Clear it here so the per-test XDG redirect
    # takes effect for every test that touches the library. The
    # collection-name and tag-union registries are keyed on the
    # collections root's mtime — a fresh tmp_path per test gives a
    # unique mtime, so the cache is naturally self-isolating across
    # tests without an explicit clear.
    from lies.library.paths import Library

    Library.open.cache_clear()
    xdg_root = tmp_path / "xdg"
    for sub in ("data", "config", "cache", "state", "runtime"):
        (xdg_root / sub).mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_root / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_root / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg_root / "cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg_root / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg_root / "runtime"))
    # Seed a minimal ``providers.toml`` at the XDG-isolated config root.
    # ``ground()`` (and other model-resolving entry points) reads
    # ``$XDG_CONFIG_HOME/<LIES_DATA_SUBDIR>/providers.toml`` via
    # ``load_providers_config()`` and raises ``ModelNotConfigured`` when
    # the file is missing. CI sandboxes have no real ``providers.toml``,
    # so without this seed every test that hits ``ground()`` fails before
    # the assertions run. The TOML declares one anthropic_compatible
    # provider (``minimax``) and every roster agent so the lookup path
    # in ``resolve_agent_to_provider`` resolves cleanly. ``indexer`` is
    # included as an extra — the TOML loader accepts unknown agents in
    # the [agents] table without complaint.
    providers_toml = xdg_root / "config" / LIES_DATA_SUBDIR / "providers.toml"
    providers_toml.parent.mkdir(parents=True, exist_ok=True)
    providers_toml.write_text(
        'default_model = "minimax:MiniMax-M3[1m]"\n'
        "\n"
        "[providers.minimax]\n"
        'type = "anthropic_compatible"\n'
        'api_key_env = "MINIMAX_API_KEY"\n'
        'base_url = "https://api.minimax.io/anthropic"\n'
        "\n"
        "[agents]\n"
        'orchestrator = "minimax:MiniMax-M3[1m]"\n'
        'source_reader = "minimax:MiniMax-M3[1m]"\n'
        'page_writer = "minimax:MiniMax-M3[1m]"\n'
        'indexer = "minimax:MiniMax-M3[1m]"\n'
        'linter = "minimax:MiniMax-M3[1m]"\n'
        'query_synthesizer = "minimax:MiniMax-M3[1m]"\n'
        'enricher = "minimax:MiniMax-M3[1m]"\n'
        'repair = "minimax:MiniMax-M3[1m]"\n'
        'librarian = "minimax:MiniMax-M3[1m]"\n',
        encoding="utf-8",
    )
    # Seed a dummy API key so ``resolve_model()`` can construct the
    # Anthropic client without raising ``ProviderConfigError`` (the
    # ``api_key_env`` referenced in the TOML above must resolve to a
    # non-empty value at read time). The value is never used for an
    # actual HTTP call — every test that exercises ``ground()`` swaps
    # out ``librarian_agent`` before ``run_sync`` fires.
    monkeypatch.setenv("MINIMAX_API_KEY", "test-key-not-real")


@pytest.fixture
def sample_wiki(tmp_path: Path) -> Wiki:
    """A copy of the sample fixture wiki initialised as a git working tree.

    Returns the :class:`Wiki` rooted at the tmp copy so tests can
    call ``synthesize_answer`` (and other wiki-scoped APIs) against a
    real on-disk corpus.
    """
    target = tmp_path / "wiki"
    shutil.copytree(FIXTURE_WIKI, target)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(target)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    return make_wiki(name="sample", data_root=target)


@pytest.fixture
def empty_wiki(tmp_path: Path) -> Wiki:
    """An empty wiki (no ``wiki/index.md``) so fallback has nothing to read."""
    target = tmp_path / "wiki"
    target.mkdir()
    (target / "raw").mkdir()
    (target / "wiki").mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(target)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    return make_wiki(name="empty", data_root=target)


@pytest.fixture
def wiki_with_missing_pages(tmp_path: Path) -> Wiki:
    """A wiki whose ``wiki/index.md`` references pages that don't exist on disk.

    Three references: two ghosts (``ghost-1.md``, ``ghost-2.md``) and one
    real page (``entities/real.md``). Only the real page should appear in
    the synthesizer's citations; the ghosts are silently skipped.
    """
    target = tmp_path / "wiki"
    shutil.copytree(FIXTURE_WIKI, target)
    (target / "wiki" / "entities").mkdir(parents=True, exist_ok=True)
    (target / "wiki" / "entities" / "real.md").write_text(
        "# Real\n\nA real page that exists on disk.\n",
        encoding="utf-8",
    )
    (target / "wiki" / "index.md").write_text(
        "# Index\n\n"
        "- [Ghost1](entities/ghost-1.md) — missing\n"
        "- [Real](entities/real.md) — exists\n"
        "- [Ghost2](concepts/ghost-2.md) — missing\n",
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(target)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "add", "."],
        cwd=target,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "fixture"],
        cwd=target,
        check=True,
        capture_output=True,
    )
    return make_wiki(name="with-missing", data_root=target)
