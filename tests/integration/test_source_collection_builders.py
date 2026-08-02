from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pymupdf
import pytest

from lies.collections.hash_manifest import HashManifest
from lies.collections.record import Collection, save_collection
from lies.etl.cost import CostBudget
from lies.etl.pipeline import SyncOrchestrator
from lies.etl.telemetry import SyncTelemetry


def _git_init(root: Path) -> None:
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(root)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "t@e.com"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True, capture_output=True)


@pytest.fixture
def wiki(tmp_path: Path) -> Path:
    root = tmp_path
    for sub in ("wiki", ".lies", "raw"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    (root / ".lies" / "collections").mkdir(parents=True, exist_ok=True)
    (root / ".lies" / "collections" / ".gitkeep").write_text("", encoding="utf-8")
    _git_init(root)
    return root


def _make_pdf(path: Path, text: str) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


def test_sync_pdf_collection_registers_ref(wiki: Path) -> None:
    pdf = wiki / "raw" / "manual.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    _make_pdf(pdf, "the quick brown fox")
    c = Collection(
        name="manual",
        path=pdf.parent,
        source=str(pdf),
        tags=[],
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1.0.0",
        created_at=datetime.now(tz=timezone.utc),
        updated_at=datetime.now(tz=timezone.utc),
        config={},
    )
    save_collection(wiki, c)
    telemetry = SyncTelemetry(c.name, wiki / ".lies" / "logs")
    manifest = HashManifest(wiki, c.name)
    budget = CostBudget()
    orch = SyncOrchestrator(
        collection=c,
        telemetry=telemetry,
        budget=budget,
        manifest=manifest,
        wiki_root=wiki,
    )
    orch.run()
    assert orch._service.is_registered("manual")
    page = wiki / "wiki" / "pages" / "page-0001.md"
    assert "the quick brown fox" in page.read_text(encoding="utf-8")
    # Wiki commit should have happened.
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=wiki, capture_output=True, text=True, check=True
    )
    assert "init" in log.stdout
    assert "sync" in log.stdout


def test_sync_liquid_collection_quarantines_everything(wiki: Path) -> None:
    liquid = wiki / "raw" / "page.liquid"
    liquid.parent.mkdir(parents=True, exist_ok=True)
    liquid.write_text("{% if x %}", encoding="utf-8")
    c = Collection(
        name="liquid_test",
        path=liquid.parent,
        source="https://example.com",
        tags=[],
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1.0.0",
        created_at=datetime.now(tz=timezone.utc),
        updated_at=datetime.now(tz=timezone.utc),
        config={},
    )
    save_collection(wiki, c)
    telemetry = SyncTelemetry(c.name, wiki / ".lies" / "logs")
    manifest = HashManifest(wiki, c.name)
    budget = CostBudget()
    orch = SyncOrchestrator(
        collection=c,
        telemetry=telemetry,
        budget=budget,
        manifest=manifest,
        wiki_root=wiki,
    )
    # Mock pick_scraper to return a scraper whose parse() yields a liquid ParsedDoc.
    from lies.scrapers.base import ParsedDoc

    fake_scraper = mock.Mock()
    fake_scraper.fetch.return_value = b""
    fake_scraper.parse.return_value = [
        ParsedDoc(
            path="page.liquid",
            content=b"{% if x %}",
            source_sha256="h",
            source_format="liquid",
        )
    ]
    fake_scraper.emit_manifest.return_value = liquid
    with mock.patch("lies.etl.stages.scrape.pick_scraper", return_value=fake_scraper):
        orch.run()
    assert telemetry.receipt().docs_quarantined == 1
    assert not any((wiki / "wiki").rglob("*"))
    assert not orch._service.is_registered("liquid_test")
