from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pymupdf
import pytest

from lies.collections.hash_manifest import HashManifest
from lies.collections.record import Collection, save_collection
from lies.etl.cost import CostBudget
from lies.etl.pipeline import SyncOrchestrator
from lies.etl.telemetry import SyncTelemetry
from lies.wiki.wiki import Wiki
from tests.conftest import make_wiki


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
def wiki(tmp_path: Path) -> Wiki:
    """Path-style wiki for stages tests + a Wiki wrapper for SyncTelemetry."""
    root = tmp_path / "wiki"
    for sub in ("wiki", "raw"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    # Seed at least one tracked file (outside wiki.wiki_dir) so the
    # initial commit is non-empty without populating the wiki content
    # directory itself.
    (root / "seed.txt").write_text("init\n", encoding="utf-8")
    _git_init(root)
    return make_wiki(name="sync", data_root=root)


def _make_pdf(path: Path, text: str) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(str(path))
    doc.close()


def test_sync_pdf_collection_registers_ref(wiki: Wiki) -> None:
    pdf = wiki.data_root / "raw" / "manual.pdf"
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
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
        config={},
    )
    save_collection(wiki, c)
    telemetry = SyncTelemetry(wiki, c.name)
    manifest = HashManifest(wiki, c.name)
    budget = CostBudget()
    orch = SyncOrchestrator(
        wiki=wiki,
        collection=c,
        telemetry=telemetry,
        budget=budget,
        manifest=manifest,
    )
    orch.run()
    assert orch._service.is_registered("manual")
    page = wiki.data_root / "wiki" / "pages" / "page-0001.md"
    assert "the quick brown fox" in page.read_text(encoding="utf-8")
    # Wiki commit should have happened.
    log = subprocess.run(
        ["git", "log", "--oneline"], cwd=wiki.data_root, capture_output=True, text=True, check=True
    )
    assert "init" in log.stdout
    assert "sync" in log.stdout


def test_sync_liquid_collection_quarantines_everything(wiki: Wiki) -> None:
    liquid = wiki.data_root / "raw" / "page.liquid"
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
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
        config={},
    )
    save_collection(wiki, c)
    telemetry = SyncTelemetry(wiki, c.name)
    manifest = HashManifest(wiki, c.name)
    budget = CostBudget()
    orch = SyncOrchestrator(
        wiki=wiki,
        collection=c,
        telemetry=telemetry,
        budget=budget,
        manifest=manifest,
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
    assert not any((wiki.data_root / "wiki").rglob("*"))
    assert not orch._service.is_registered("liquid_test")


def test_sync_htmx_sphinx_with_excludes(wiki: Wiki) -> None:
    """htmx-style Sphinx collection: three rst docs; ``sphinx_excludes``
    filters two of them; only the kept file is written to ``wiki/``.
    The ``WikiCollectionRef`` is registered; exactly one atomic commit
    lands on top of the fixture init.

    This pins the C1 fix (bespoke routing through
    ``BespokeBuilder.build(workspace, collection)``), the I4/I5 fix
    (collision-resistant subdir + cleanup), and the I8 fix
    (registration does not over-count docs).

    The pipeline runs end-to-end: SCRAPE → NORMALIZE → WRITE →
    REGISTER. The WRITE stage's post-commit hook invokes
    ``qmd collection add`` (idempotent), ``qmd update``, and rebuilds
    ``wiki/index.md``. ``BespokeBuilder.build`` is mocked to a
    side-effect that walks the synth manifest and applies the
    configured ``sphinx_excludes`` — the same dispatch contract a
    real SphinxBuilder would implement.
    """
    import hashlib
    import json
    import re
    from datetime import datetime

    from lies.builders.bespoke import BespokeBuilder
    from lies.scrapers.base import ParsedDoc

    def _sha(content: bytes) -> str:
        return hashlib.sha256(content).hexdigest()

    keep_body = b"Title\n=====\n\nBody.\n"
    template_body = b"Template\n=========\n\nnope.\n"
    example_body = b"Example\n=======\n\nnope.\n"

    # Use bare basenames because the synth per-doc manifest in
    # NORMALIZE only carries the basename of ``doc.path``. Sphinx
    # excludes that glob the basename filter correctly.
    docs = [
        ParsedDoc(
            path="index.rst",
            content=keep_body,
            source_sha256=_sha(keep_body),
            source_format="bespoke",
        ),
        ParsedDoc(
            path="base_template.rst",
            content=template_body,
            source_sha256=_sha(template_body),
            source_format="bespoke",
        ),
        ParsedDoc(
            path="demo_example.rst",
            content=example_body,
            source_sha256=_sha(example_body),
            source_format="bespoke",
        ),
    ]

    raw_dir = wiki.data_root / "raw" / "htmx"
    raw_dir.mkdir(parents=True, exist_ok=True)
    c = Collection(
        name="htmx",
        path=raw_dir,
        source="https://github.com/bigskysoftware/htmx/tree/master/www/content",
        tags=["htmx"],
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1.0.0",
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
        config={
            "sphinx_includes": ["*.rst"],
            "sphinx_excludes": ["base_template.rst", "demo_example.rst"],
        },
    )
    save_collection(wiki, c)

    fake_scraper = mock.Mock()
    fake_scraper.fetch.return_value = b""
    fake_scraper.parse.return_value = list(docs)

    def fake_emit(parsed: list[ParsedDoc], dest: Path) -> Path:
        dest.mkdir(parents=True, exist_ok=True)
        for d in parsed:
            (dest / d.path).parent.mkdir(parents=True, exist_ok=True)
            (dest / d.path).write_bytes(d.content)
        manifest = {
            "files": [
                {
                    "path": d.path,
                    "out_path": d.path.removesuffix(".rst") + ".md",
                    "source_format": "markdown",
                    "sha256": d.source_sha256,
                }
                for d in parsed
            ]
        }
        (dest / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return dest / "manifest.json"

    fake_scraper.emit_manifest.side_effect = fake_emit

    def fake_per_doc_build(
        _self: object, workspace: Path, *, collection: Collection
    ) -> list[ParsedDoc]:
        cfg = collection.config or {}
        excludes: list[str] = list(cfg.get("sphinx_excludes", []))
        manifest_path = workspace / "manifest.json"
        if not manifest_path.exists():
            return []
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        out: list[ParsedDoc] = []
        for entry in manifest.get("files", []):
            rel_path = entry["path"]
            target = entry.get("out_path", rel_path)
            if any(Path(rel_path).match(g) for g in excludes):
                continue
            src = workspace / rel_path
            if not src.exists():
                continue
            content = src.read_bytes()
            text = content.decode("utf-8", errors="replace")
            md = re.sub(r"=+\n", lambda _: "", text)
            md = re.sub(r"^([A-Za-z][^\n]*)\n=+\n", r"# \1\n\n", md, count=1, flags=re.MULTILINE)
            encoded = md.encode("utf-8")
            out.append(
                ParsedDoc(
                    path=target,
                    content=encoded,
                    source_sha256=_sha(encoded),
                    source_format="markdown",
                )
            )
        return out

    telemetry = SyncTelemetry(wiki, c.name)
    manifest = HashManifest(wiki, c.name)
    budget = CostBudget()
    orch = SyncOrchestrator(
        wiki=wiki,
        collection=c,
        telemetry=telemetry,
        budget=budget,
        manifest=manifest,
    )

    # C1 fix pinned: every bespoke ParsedDoc routes through
    # ``REGISTRY.resolve("bespoke").build(workspace, collection)``.
    with (
        mock.patch("lies.etl.stages.scrape.pick_scraper", return_value=fake_scraper),
        mock.patch.object(BespokeBuilder, "build", autospec=True, side_effect=fake_per_doc_build),
    ):
        orch.run()

    written = sorted(
        p.relative_to(wiki.data_root).as_posix()
        for p in (wiki.data_root / "wiki").rglob("*")
        if p.is_file()
    )
    # Only the kept file is written; excludes filtered out the others.
    assert "wiki/index.rst" in written, f"index.rst missing in {written!r}"
    assert "wiki/base_template.rst" not in written, (
        f"exclude did not filter base_template.rst: {written!r}"
    )
    assert "wiki/demo_example.rst" not in written, (
        f"exclude did not filter demo_example.rst: {written!r}"
    )

    receipt = telemetry.receipt()
    # I8 fix pinned: registration does not over-count docs.
    assert receipt.docs_quarantined == 2, (
        f"expected 2 quarantined (templates + examples), got {receipt.docs_quarantined}"
    )

    # WikiCollectionRef is registered exactly once.
    assert orch._service.is_registered("htmx")
    # Exactly one sync commit lands on top of the fixture init.
    log_out = subprocess.run(
        ["git", "log", "--oneline"], cwd=wiki.data_root, capture_output=True, text=True, check=True
    )
    assert log_out.stdout.count("\n") == 2  # one init + one sync commit
