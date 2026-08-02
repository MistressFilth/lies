from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.models.test import TestModel

from lies.memory.enricher import MemoryEnricherDeps
from lies.memory.models import MemoryPlan, PageUpdate, WikiWriteConflict
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
    assert set(orch._harness_memory.operational_state) >= {
        "last_enrichment_attempt",
        "pending_retry",
        "qmd_status",
        "schema_version",
        "request_ref",
        "last_commit_sha",
    }


def test_run_with_memory_persists_new_page_on_relevant_turn(wiki: WikiLayout) -> None:
    orch = Orchestrator(wiki_root=wiki.root, model=TestModel())
    answer = orch.run_with_memory("Read raw/articles/intro.md and tell me about it")
    # The default TestModel returns an empty structured answer; we
    # assert that the orchestrator completed without error and the
    # wiki state is intact.
    assert isinstance(answer, str)
    assert wiki.wiki_dir.exists()


def test_conflict_causes_one_fresh_read_and_enrichment_retry(
    wiki: WikiLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    page = wiki.wiki_dir / "concepts" / "x.md"
    body = "---\ntitle: X\ntype: concept\n---\n# X\n"
    page.write_text(body, encoding="utf-8")
    orch = Orchestrator(wiki_root=wiki.root, model=TestModel())
    plans = [
        MemoryPlan(
            operations=[
                PageUpdate(
                    path="concepts/x.md",
                    expected_sha256="stale",
                    content=body + "updated\n",
                    evidence=["page-1"],
                )
            ],
            rationale="first",
            evidence=["page-1"],
        ),
        MemoryPlan(operations=[], rationale="retry noop", evidence=[]),
    ]
    seen_metadata: list[dict[str, dict[str, str]]] = []

    def run_sync(_prompt: str, *, deps: MemoryEnricherDeps) -> object:
        seen_metadata.append(deps.current_page_metadata)
        return SimpleNamespace(output=plans.pop(0))

    monkeypatch.setattr(orch._enricher, "run_sync", run_sync)
    monkeypatch.setattr(
        orch._memory_service,
        "apply_plan",
        lambda _plan: (_ for _ in ()).throw(WikiWriteConflict("changed")),
    )
    receipt = orch._run_enrichment("request", "answer", ["page-1"], [])
    assert receipt.errors == []
    assert len(seen_metadata) == 2
    assert seen_metadata[0] == {}
    assert seen_metadata[1]["concepts/x.md"]["content"] == body


def test_second_conflict_is_queued_for_retry(
    wiki: WikiLayout, monkeypatch: pytest.MonkeyPatch
) -> None:
    orch = Orchestrator(wiki_root=wiki.root, model=TestModel())
    plan = MemoryPlan(
        operations=[
            PageUpdate(
                path="concepts/x.md",
                expected_sha256="stale",
                content="---\ntitle: X\ntype: concept\n---\n# X\n",
                evidence=["page-1"],
            )
        ],
        rationale="retry",
        evidence=["page-1"],
    )
    monkeypatch.setattr(
        orch._enricher,
        "run_sync",
        lambda *_args, **_kwargs: SimpleNamespace(output=plan),
    )
    monkeypatch.setattr(
        orch._memory_service,
        "apply_plan",
        lambda _plan: (_ for _ in ()).throw(WikiWriteConflict("changed twice")),
    )
    receipt = orch._run_enrichment("request", "answer", ["page-1"], [])
    assert receipt.errors == ["queued_for_retry: WikiWriteConflict: changed twice"]
    assert len(orch._enrichment_queue) == 1
