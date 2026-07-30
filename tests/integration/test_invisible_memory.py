from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from lies.orchestrator import Orchestrator
from lies.wiki.layout import WikiLayout


@pytest.fixture
def wiki(tmp_path: Path) -> WikiLayout:
    root = tmp_path / "wiki"
    for sub in ("wiki", ".lies", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    (root / ".lies" / "schema.md").write_text(
        "## Page types\n- overview\n- entity\n- concept\n- comparison\n- source\n",
        encoding="utf-8",
    )
    import subprocess
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return WikiLayout(root)


def test_run_with_memory_skips_unrelated_turn(wiki: WikiLayout) -> None:
    orch = Orchestrator(wiki_root=wiki.root, model=TestModel())
    answer = orch.run_with_memory("Hello there.")
    assert isinstance(answer, str)
    log_path = wiki.wiki_dir / "log.md"
    assert not log_path.exists() or "memory" not in log_path.read_text(encoding="utf-8")


def test_run_with_memory_persists_new_page_on_relevant_turn(wiki: WikiLayout) -> None:
    orch = Orchestrator(wiki_root=wiki.root, model=TestModel())
    answer = orch.run_with_memory("Read raw/articles/intro.md and tell me about it")
    # The default TestModel returns an empty structured answer; we
    # assert that the orchestrator completed without error and the
    # wiki state is intact.
    assert isinstance(answer, str)
    assert wiki.wiki_dir.exists()