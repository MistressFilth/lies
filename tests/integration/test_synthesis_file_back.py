"""Integration tests for F3 synthesis file-back. Gated on INTEGRATION=1.

Exercises the end-to-end file-back flow against a real wiki + real
qmd daemon + cross-process flock:

1. Two consecutive ``Orchestrator.run_query`` calls with the same
   question produce a deterministic slug; the first creates a
   synthesis page under ``wiki/<collection>/synthesis/<slug>.md`` and
   the second updates it at the same path.
2. A held cross-process flock across the call exhausts the
   file-back retry budget (3 inline attempts) and surfaces a
   ``file_back_failed_after_3_attempts`` error in the receipt.

Run with:

    INTEGRATION=1 pytest tests/integration/test_synthesis_file_back.py -v

Without ``INTEGRATION=1``, the module skips cleanly so the test does
not couple the unit suite to a live qmd daemon or to the cross-process
flock. The lock-holding scenario uses the public
:func:`acquire_create_lock` primitive + a subprocess holder (matching
the ``tests/integration/test_memory_lock.py`` shape) — ``WikiMemoryService``
offers ``_lock`` (an in-process ``threading.Lock``) but the cross-process
flock the orchestrator's file-back path actually races is the
``acquire_create_lock`` envelope, so that's what the busy scenario
exercises.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from lies.orchestrator import Orchestrator
from lies.wiki.wiki import Wiki
from tests.conftest import make_wiki, models_for_tests

pytestmark = pytest.mark.skipif(
    os.environ.get("INTEGRATION") != "1",
    reason="gated on INTEGRATION=1 (real qmd daemon + flock)",
)


@pytest.fixture
def wiki_dir(tmp_path: Path) -> Wiki:
    """A real git wiki rooted in ``tmp_path`` with an index page that
    references at least one concept page.

    Mirrors the fixture shape used by ``test_lint_repair`` and
    ``test_invisible_memory``: ``make_wiki`` wires the five XDG roots
    under the active ``_isolated_xdg`` fixture (parent ``tests/conftest.py``)
    so the orchestrator + WikiMemoryService resolve the wiki through
    the same code paths a real ``lies init`` would set up.

    The qmd daemon is *not* started here — the operator commits to
    ``INTEGRATION=1`` only when one is already running locally, and the
    index page keeps the ``wiki/index.md`` fallback useful when qmd
    cannot be reached.
    """
    root = tmp_path / "wiki"
    for sub in ("wiki", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "concepts" / "hooks.md").write_text(
        "---\ntitle: Hooks\ntype: concept\n---\n"
        "# Hooks\n\nA hook intercepts events at fixed points.\n",
        encoding="utf-8",
    )
    (root / "wiki" / "index.md").write_text(
        "# Index\n\n- [Hooks](concepts/hooks.md) — event interception points\n",
        encoding="utf-8",
    )
    wiki = make_wiki(name="synthesis-file-back", data_root=root)
    wiki.config_root.mkdir(parents=True, exist_ok=True)
    (wiki.config_root / "schema.md").write_text(
        "## Page types\n- concept\n- synthesis\n- entity\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return wiki


def test_two_queries_with_same_question_collide_to_page_update(wiki_dir: Wiki) -> None:
    """First query writes PageCreate; second writes PageUpdate at the same slug.

    The deterministic slug derived from the question (``<sha256[:8]>-<text>``)
    means the second ``run_query`` resolves to a ``PageUpdate`` against the
    page the first call just created. Both file-receipts report zero
    errors.
    """
    orch = Orchestrator(wiki=wiki_dir, models=models_for_tests(TestModel()))

    ans_a = orch.run_query(
        "what is a hook?",
        collection="claude-code",
        force_file=True,
    )
    assert ans_a.file_receipt is not None
    assert ans_a.file_receipt.errors == []
    # The first call materializes a synthesis page under the collection.
    create_paths = [
        ref.path for ref in ans_a.file_receipt.changed_pages if ref.op.value == "create"
    ]
    assert create_paths, f"expected a create op on first call, got {ans_a.file_receipt!r}"
    expected_slug = create_paths[0]

    ans_b = orch.run_query(
        "what is a hook?",
        collection="claude-code",
        force_file=True,
    )
    assert ans_b.file_receipt is not None
    assert ans_b.file_receipt.errors == []
    # Second call hits the same slug → PageUpdate at the same relative path.
    update_paths = [
        ref.path for ref in ans_b.file_receipt.changed_pages if ref.op.value == "update"
    ]
    assert update_paths, f"expected an update op on second call, got {ans_b.file_receipt!r}"
    assert update_paths[0] == expected_slug


# ---------------------------------------------------------------------------
# Lock-busy scenario — held cross-process flock across the call
# ---------------------------------------------------------------------------


_HOLDER_SCRIPT = textwrap.dedent(
    """
    import os, sys, time
    from pathlib import Path

    from lies.utils.exclusive import acquire_create_lock
    from lies.utils.lock_heartbeat import Heartbeat, write_heartbeat, write_owner_pid

    create_lock = Path(sys.argv[1])
    pid_path = Path(sys.argv[2])
    state_path = Path(sys.argv[3])
    ready_marker = Path(sys.argv[4])

    create_lock.parent.mkdir(parents=True, exist_ok=True)
    result = acquire_create_lock(
        create_lock,
        max_age_s=7200,
        pid_path=pid_path,
        state_json_path=state_path,
    )
    if result is None:
        sys.exit(2)
    write_owner_pid(pid_path, os.getpid())
    write_heartbeat(
        state_path,
        Heartbeat(pid=os.getpid(), started_at=time.time(), scope=""),
    )
    ready_marker.write_text("ready", encoding="utf-8")
    time.sleep(15)
    """
)


def _wait_for_holder(ready_marker: Path, *, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while not ready_marker.exists():
        if time.time() > deadline:
            pytest.fail("holder process did not signal ready in time")
        time.sleep(0.05)


def test_lock_busy_simulated_yields_three_attempts(wiki_dir: Wiki, tmp_path: Path) -> None:
    """Held flock across the call → 3 inline attempts → ``file_back_failed_after_3_attempts``.

    The holder subprocess takes the cross-process flock via the public
    :func:`acquire_create_lock` primitive and keeps it for 15 s. While
    the lock is held, ``WikiMemoryService.apply_plan`` raises
    :class:`WikiLockBusy` on every attempt; ``Orchestrator.file_back_synthesis``
    retries inline 3 times before returning a receipt whose
    ``errors`` carry the documented ``file_back_failed_after_3_attempts``
    marker.
    """
    create_lock = wiki_dir.memory_create_lock_path
    pid_path = wiki_dir.memory_pid_path
    state_path = wiki_dir.memory_heartbeat_path
    ready_marker = tmp_path / "holder.ready"

    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _HOLDER_SCRIPT,
            str(create_lock),
            str(pid_path),
            str(state_path),
            str(ready_marker),
        ],
    )
    try:
        _wait_for_holder(ready_marker)

        orch = Orchestrator(wiki=wiki_dir, models=models_for_tests(TestModel()))
        ans = orch.run_query(
            "what is a hook?",
            collection="claude-code",
            force_file=True,
        )
    finally:
        holder.terminate()
        try:
            holder.wait(timeout=10)
        except subprocess.TimeoutExpired:
            holder.kill()
            holder.wait(timeout=5)

    assert ans.file_receipt is not None
    assert any("file_back_failed_after_3_attempts" in err for err in ans.file_receipt.errors), (
        f"expected lock-busy error after 3 attempts, got {ans.file_receipt.errors!r}"
    )
    assert ans.file_receipt.changed_pages == []

    # Cleanup: the holder's terminate releases the flock; no leftover state.
    shutil.rmtree(wiki_dir.runtime_root, ignore_errors=True)
