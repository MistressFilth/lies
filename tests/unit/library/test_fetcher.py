"""Tests for ScraperFetcher (concrete Fetcher wrapping existing scrapers)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from lies.etl.normalize.format_dispatch import UnknownFormatError
from lies.library.errors import LibraryFetchUnreachable
from lies.library.fetcher import ScraperFetcher, _load_bespoke_scraper
from lies.library.ingest import FetchItem
from lies.scrapers.base import BaseScraper, ParsedDoc


class _FakeScraper:
    """Fake scraper exposing the real ``fetch`` + ``parse`` interface."""

    def __init__(self, docs: list[ParsedDoc]) -> None:
        self._docs = docs
        self.fetch_called_with: list[object] = []
        self.parse_called_with: list[object] = []

    def fetch(self, source: Path | str) -> bytes:
        self.fetch_called_with.append(source)
        return b"raw bytes"

    def parse(
        self,
        raw: bytes,
        *,
        source: Path | str | None = None,
    ) -> list[ParsedDoc]:
        self.parse_called_with.append(source)
        return self._docs


def test_fetcher_source_hash_is_raw_bytes(monkeypatch, tmp_path: Path) -> None:
    """C4 anti-tautology: source_hash is SHA256 of raw fetch bytes, not per-doc slices.

    Two scrapers returning different per-doc ``source_sha256`` /
    ``content`` parses for the same source URL must produce the SAME
    ``source_hash`` on the emitted ``FetchItem`` (spec §Frontmatter).
    The hash is computed once from the raw scraper.fetch() bytes,
    not from doc.source_sha256 or doc.content.
    """
    import hashlib

    raw_bytes = b"shared raw response body across both scrapers"

    class _ScraperA:
        def fetch(self, source):  # type: ignore[no-untyped-def]
            return raw_bytes

        def parse(self, raw, *, source=None):  # type: ignore[no-untyped-def]
            from lies.scrapers.base import ParsedDoc

            return [
                ParsedDoc(
                    path="a.md",
                    content=b"per-doc content A",
                    source_sha256="aa" * 32,  # DIFFERENT from raw bytes
                    source_format="markdown",
                )
            ]

    class _ScraperB:
        def fetch(self, source):  # type: ignore[no-untyped-def]
            return raw_bytes

        def parse(self, raw, *, source=None):  # type: ignore[no-untyped-def]
            from lies.scrapers.base import ParsedDoc

            return [
                ParsedDoc(
                    path="a.md",
                    content=b"per-doc content B",
                    source_sha256="bb" * 32,  # DIFFERENT again
                    source_format="markdown",
                )
            ]

    # Run with scraper A — capture the hash.
    monkeypatch.setattr("lies.scrapers.base.pick_scraper", lambda source: _ScraperA())
    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    items_a = list(fetcher.fetch_sources("https://example.com/x"))

    # Re-run with scraper B — assert the hash matches A.
    monkeypatch.setattr("lies.scrapers.base.pick_scraper", lambda source: _ScraperB())
    items_b = list(fetcher.fetch_sources("https://example.com/x"))

    expected_hash = hashlib.sha256(raw_bytes).hexdigest()
    assert items_a[0].source_hash == expected_hash
    assert items_b[0].source_hash == expected_hash
    assert items_a[0].source_hash == items_b[0].source_hash, (
        "source_hash must be stable across scrapers returning different per-doc parses"
    )


def test_fetcher_invokes_local_file(monkeypatch, tmp_path: Path) -> None:
    """ScraperFetcher drives a fake scraper end-to-end and yields FetchItems.

    Covers the brief's primary happy-path: pick_scraper + fetch + parse +
    emit FetchItem with source_hash + fetched_via populated.
    """
    src = tmp_path / "page.md"
    src.write_text("# hello\nbody\n")
    fake = _FakeScraper(
        [
            ParsedDoc(
                path="page.md",
                content=b"raw body",
                source_sha256="abc123def456",
                source_format="markdown",
            )
        ]
    )
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )

    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    items = list(fetcher.fetch_sources(src))

    assert len(items) == 1
    item = items[0]
    # C4: source_hash is SHA256 of raw scraper.fetch() bytes — NOT
    # the per-doc source_sha256 ("abc123def456") carried on ParsedDoc.
    import hashlib

    expected_raw_hash = hashlib.sha256(b"raw bytes").hexdigest()
    assert item.source_hash == expected_raw_hash
    assert item.source_hash != "abc123def456", (
        "source_hash must be raw-bytes hash, not the per-doc source_sha256"
    )
    assert item.fetched_via == "_FakeScraper"
    assert item.body == "raw body"
    assert item.path == Path("page.md")
    # Source was a Path, not a URL — url stays None per FetchItem contract.
    assert item.url is None
    # Scraper's fetch + parse were both invoked with the source.
    assert fake.fetch_called_with == [src]
    assert fake.parse_called_with == [src]


def test_fetcher_url_source_sets_url_field(monkeypatch) -> None:
    """When source is a str (URL), ``FetchItem.url`` carries it.

    ``path`` is still derived from the parsed doc's relative path. Mirrors
    the existing wiki-side ingest semantics where URL fetches leave path
    empty and route via URL instead.
    """
    fake = _FakeScraper(
        [
            ParsedDoc(
                path="chunk-0000.md",
                content=b"hello",
                source_sha256="deadbeef",
                source_format="markdown",
            )
        ]
    )
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )

    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    items = list(fetcher.fetch_sources("https://example.com/page"))

    assert len(items) == 1
    assert items[0].url == "https://example.com/page"
    assert items[0].path == Path("chunk-0000.md")


def test_fetcher_emits_multiple_docs(monkeypatch) -> None:
    """Scrapers yielding multiple docs → multiple FetchItems.

    E.g. an llms.txt index page produces one item per linked page.
    """
    fake = _FakeScraper(
        [
            ParsedDoc(
                path="a.md",
                content=b"aaa",
                source_sha256="aa" * 32,
                source_format="markdown",
            ),
            ParsedDoc(
                path="b.md",
                content=b"bbb",
                source_sha256="bb" * 32,
                source_format="markdown",
            ),
            ParsedDoc(
                path="c.md",
                content=b"ccc",
                source_sha256="cc" * 32,
                source_format="markdown",
            ),
        ]
    )
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )

    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    items = list(fetcher.fetch_sources("https://example.com/x"))

    assert [i.path.name for i in items] == ["a.md", "b.md", "c.md"]
    # C4: every emitted item carries the SAME source_hash (raw bytes
    # hash), regardless of per-doc source_sha256 differences.
    assert len({i.source_hash for i in items}) == 1, (
        f"all items must share one source_hash (raw bytes); got {[i.source_hash for i in items]}"
    )


def test_fetcher_derives_hash_from_raw_bytes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """C4: source_hash is SHA256 of the raw scraper.fetch() bytes, not per-doc.

    The previous contract used ``doc.source_sha256 or _hash_bytes(doc.content)``
    (per-doc slices) which mixed scraper-specific parser output into the
    hash. Spec §Frontmatter requires SHA256 of raw bytes at fetch time,
    so the fetcher now hashes the upstream ``scraper.fetch(source)``
    output and threads that single value through every emitted item.
    """
    src = tmp_path / "page.md"
    src.write_text("# hello\nbody\n")
    raw_bytes = b"raw scraper.fetch() bytes"
    content = b"\x00\x01\x02 deterministic bytes"
    fake = _FakeScraper(
        [
            ParsedDoc(
                path="page.md",
                content=content,
                source_sha256="",
                source_format="markdown",
            )
        ]
    )
    # _FakeScraper.fetch returns b"raw bytes" — use a custom raw_bytes
    # by overriding the fake's fetch at construction time.
    fake.fetch = lambda source: raw_bytes  # type: ignore[method-assign]
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )

    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    items = list(fetcher.fetch_sources(src))

    import hashlib

    assert items[0].source_hash == hashlib.sha256(raw_bytes).hexdigest()
    # The doc.content hash is NOT the source_hash (per spec §Frontmatter).
    assert items[0].source_hash != hashlib.sha256(content).hexdigest()


def test_fetcher_zero_items_raises_library_fetch_unreachable(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Zero-emission scraper → ``LibraryFetchUnreachable`` (Task 7 error)."""
    src = tmp_path / "page.md"
    src.write_text("# hello\nbody\n")
    fake = _FakeScraper([])
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )

    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    with pytest.raises(LibraryFetchUnreachable):
        list(fetcher.fetch_sources(src))


def test_fetcher_yields_iterator_not_list(monkeypatch, tmp_path: Path) -> None:
    """Brief contract: ``fetch_sources`` returns an ``Iterator[FetchItem]``.

    Generator semantics matter so the ingest pipeline can stream large
    batches without materializing them.
    """
    src = tmp_path / "page.md"
    src.write_text("# hello\nbody\n")
    fake = _FakeScraper(
        [
            ParsedDoc(
                path="page.md",
                content=b"hi",
                source_sha256="ff" * 32,
                source_format="markdown",
            )
        ]
    )
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )

    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    result = fetcher.fetch_sources(src)
    assert isinstance(result, Iterator)
    # Pulling consumes the generator — confirm items flow before exhaustion.
    first = next(result)
    assert isinstance(first, FetchItem)


def test_fetcher_unknown_format_propagates(monkeypatch, tmp_path: Path) -> None:
    """A REGISTRY-registered format without ``Collection`` propagates a typed BuilderError.

    ``liquid`` IS registered with REGISTRY (via the ``LiquidBuilder``
    module side-effect import), but the builder reads
    ``collection.config`` — without a Collection the builder raises
    ``AttributeError`` on the bare-``None``. Minor 30 wraps that
    ``AttributeError`` in a typed ``BuilderError`` so the operator sees
    "builder for 'liquid' requires a Collection" instead of a confusing
    ``AttributeError: 'NoneType' object has no attribute 'config'``.
    The ingest pipeline handles quarantine.
    """
    from lies.builders.errors import BuilderError

    src = tmp_path / "page.md"
    src.write_text("# hello\nbody\n")
    fake = _FakeScraper(
        [
            ParsedDoc(
                path="page.md",
                content=b"<html>raw html bytes</html>",
                source_sha256="aa" * 32,
                source_format="liquid",
            )
        ]
    )
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )

    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    with pytest.raises(BuilderError, match="requires a Collection"):
        list(fetcher.fetch_sources(src))


def test_fetcher_unrecognized_format_propagates(monkeypatch, tmp_path: Path) -> None:
    """An arbitrary unknown source_format propagates ``UnknownFormatError``.

    Guards the catch-all branch of ``format_dispatch.dispatch`` (anything
    not in the markdown/html/rst/pdf/liquid whitelist raises). Without
    this propagation, raw content could silently land as a mirror file.
    """
    src = tmp_path / "page.md"
    src.write_text("# hello\nbody\n")
    fake = _FakeScraper(
        [
            ParsedDoc(
                path="page.md",
                content=b"\x00\x01\x02 binary blob",
                source_sha256="bb" * 32,
                source_format="x-totally-made-up",
            )
        ]
    )
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )

    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    with pytest.raises(UnknownFormatError):
        list(fetcher.fetch_sources(src))


# ---------------------------------------------------------------------------
# Finding 1: scraper_cmd must be honored end-to-end.
# ---------------------------------------------------------------------------


class _FakeBespokeScraper:
    """Minimal BaseScraper for the bespoke-loader happy path."""

    def fetch(self, source: Path | str) -> bytes:
        return b"raw bytes from bespoke"

    def parse(
        self,
        raw: bytes,
        *,
        source: str | Path | None = None,
    ) -> list[ParsedDoc]:
        return [
            ParsedDoc(
                path="page.md",
                content=b"# bespoke body\n",
                source_sha256="bb" * 32,
                source_format="markdown",
            )
        ]

    def emit_manifest(self, docs: list[ParsedDoc], raw_dir: Path) -> Path:
        return raw_dir / "manifest.json"


def test_fetcher_uses_bespoke_loader_when_scraper_cmd_set(monkeypatch, tmp_path: Path) -> None:
    """``scraper_cmd`` routes through ``_load_bespoke_scraper`` instead of ``pick_scraper``.

    Pin for Finding 1: the bespoke scraper (loaded from
    ``module:attr``) must drive the fetch/parse, NOT the URL/path
    prefix heuristic. We monkeypatch the bespoke loader to return a
    fake scraper and assert ``pick_scraper`` was never called.
    """
    fake = _FakeBespokeScraper()
    pick_called = {"n": 0}

    def _fake_pick(source):
        pick_called["n"] += 1
        return fake

    def _fake_load(spec):
        assert spec == "my_pkg.mod:scraper"
        return fake

    monkeypatch.setattr("lies.scrapers.base.pick_scraper", _fake_pick)
    monkeypatch.setattr("lies.library.fetcher._load_bespoke_scraper", _fake_load)

    src = tmp_path / "page.md"
    src.write_text("# hello\n")
    fetcher = ScraperFetcher(
        library=None,  # type: ignore[arg-type]
        scraper_cmd="my_pkg.mod:scraper",
    )
    items = list(fetcher.fetch_sources(src))

    assert len(items) == 1
    assert items[0].fetched_via == "_FakeBespokeScraper"
    assert pick_called["n"] == 0, "pick_scraper must NOT be called when scraper_cmd is set"


def test_fetcher_propagates_bespoke_loader_failure(monkeypatch, tmp_path: Path) -> None:
    """A broken bespoke loader propagates; the fetcher never falls through.

    Pin for Finding 1's fail-loud contract: a misconfigured
    ``scraper_cmd`` (``bad:attr`` or a non-importable module) raises
    ``ScraperUnavailable`` from ``_load_bespoke_scraper`` and the
    fetcher does NOT silently route through ``pick_scraper``.

    Minor 44: the bespoke-loader failure is wrapped in
    ``LibraryFetchUnreachable`` (a ``LibraryError`` subclass) so it
    appears under the same taxonomy as every other library error.
    The test asserts both the typed lineage and the cause.
    """
    pick_called = {"n": 0}

    def _fake_pick(source):
        pick_called["n"] += 1
        return _FakeBespokeScraper()

    def _fake_load(spec):
        from lies.scrapers.errors import ScraperUnavailable

        raise ScraperUnavailable(f"bad loader for {spec!r}")

    monkeypatch.setattr("lies.scrapers.base.pick_scraper", _fake_pick)
    monkeypatch.setattr("lies.library.fetcher._load_bespoke_scraper", _fake_load)

    src = tmp_path / "page.md"
    src.write_text("# hello\n")
    fetcher = ScraperFetcher(
        library=None,  # type: ignore[arg-type]
        scraper_cmd="bad:attr",
    )
    with pytest.raises(LibraryFetchUnreachable) as ei:
        list(fetcher.fetch_sources(src))
    # pick_scraper must never be reached — fail-loud contract.
    assert pick_called["n"] == 0
    assert "bad loader" in str(ei.value)
    assert "bespoke scraper loader failed" in str(ei.value)
    # ``ScraperUnavailable`` is preserved on ``__cause__`` so the
    # bespoke-loader lineage is still inspectable.
    assert isinstance(ei.value.__cause__, Exception)


def test_load_bespoke_scraper_rejects_missing_colon() -> None:
    """``module:attr`` shape is enforced — no colon means fail loud.

    Minor 44: the bare ``_load_bespoke_scraper`` raises
        ``ScraperUnavailable``; the wrapping into
        ``LibraryFetchUnreachable`` happens at the call site
        (``fetch_sources``), not inside the function.
    """
    from lies.scrapers.errors import ScraperUnavailable

    with pytest.raises(ScraperUnavailable, match="module:attr"):
        _load_bespoke_scraper("no_colon_here")


def test_fetcher_fetched_via_uses_short_id(monkeypatch, tmp_path: Path) -> None:
    """Minor 31: ``fetched_via`` uses short id (``web``/``github``/``pdf``).

    The previous implementation used ``type(scraper).__name__`` (e.g.
    ``WebScraper``), leaking the class hierarchy into the mirror
    frontmatter. The short-id contract pins the cross-task value to
    ``web`` / ``github`` / ``pdf`` regardless of the concrete class.
    """
    from lies.scrapers.github import GitHubScraper
    from lies.scrapers.pdf import PDFScraper
    from lies.scrapers.web import WebScraper

    def _stub_methods(instance: BaseScraper) -> None:
        # Override network-touching methods so the test does not hit
        # the real web scraper's llms.txt heuristic.
        instance.fetch = lambda source: b"raw body"  # type: ignore[method-assign]
        instance.parse = lambda raw, *, source=None: [  # type: ignore[method-assign]
            ParsedDoc(
                path="x.md",
                content=b"body",
                source_sha256="aa" * 32,
                source_format="markdown",
            )
        ]

    for cls, expected_short in [
        (WebScraper, "web"),
        (GitHubScraper, "github"),
        (PDFScraper, "pdf"),
    ]:
        instance = cls()
        _stub_methods(instance)
        monkeypatch.setattr("lies.scrapers.base.pick_scraper", lambda source, _i=instance: _i)
        fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
        items = list(fetcher.fetch_sources("https://example.com/x"))
        assert items[0].fetched_via == expected_short, (
            f"{cls.__name__} should map to {expected_short!r}; got {items[0].fetched_via!r}"
        )


def test_fetcher_fetched_via_unknown_scraper_falls_back_to_classname(
    monkeypatch, tmp_path: Path
) -> None:
    """Minor 31: an unmapped scraper class falls back to ``type(scraper).__name__``.

    Keeps the field populated even when a new bespoke scraper class is
    added without registering a short id — the operator still gets a
    non-empty marker they can grep for rather than a missing field.
    """
    from lies.scrapers.base import ParsedDoc

    class _UserScraper(BaseScraper):
        def fetch(self, source):  # type: ignore[no-untyped-def]
            return b"raw"

        def parse(self, raw, *, source=None):  # type: ignore[no-untyped-def]
            return [
                ParsedDoc(
                    path="x.md",
                    content=b"body",
                    source_sha256="aa" * 32,
                    source_format="markdown",
                )
            ]

        def emit_manifest(self, docs, raw_dir):  # type: ignore[no-untyped-def]
            return raw_dir / "manifest.json"

    fake = _UserScraper()
    monkeypatch.setattr("lies.scrapers.base.pick_scraper", lambda source: fake)
    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    items = list(fetcher.fetch_sources("https://example.com/x"))
    assert items[0].fetched_via == "_UserScraper"


def test_normalize_body_missing_collection_wraps_attribute_error(
    monkeypatch, tmp_path: Path
) -> None:
    """Minor 30: a builder reading ``collection.config`` without a Collection
    surfaces a typed ``BuilderError`` rather than a confusing
    ``AttributeError``.

    Previously the bare ``AttributeError`` was caught by
    ``_iter_fetch_items``'s quarantine branch, and the operator saw a
    ``fetch-unreachable:NoneType has no attribute 'config'`` reason
    instead of a clean "collection required" diagnostic.
    """
    from lies.builders.base import REGISTRY
    from lies.builders.errors import BuilderError
    from lies.scrapers.base import ParsedDoc

    class _CollectionReadingBuilder:
        def build(self, workspace: Path, *, collection):  # type: ignore[no-untyped-def]
            # Mimic a builder that reads ``collection.config`` — the bare
            # ``None`` raises ``AttributeError`` here.
            return collection.config["k"]

    formats = set(REGISTRY.formats()) | {"x-coll-required"}
    monkeypatch.setattr(REGISTRY, "formats", lambda: formats)
    monkeypatch.setattr(REGISTRY, "resolve", lambda fmt: _CollectionReadingBuilder())

    fake = _FakeScraper(
        [
            ParsedDoc(
                path="page.x-coll-required",
                content=b"raw",
                source_sha256="aa" * 32,
                source_format="x-coll-required",
            )
        ]
    )
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )
    src = tmp_path / "page.x-coll-required"
    src.write_bytes(b"placeholder")

    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    with pytest.raises(BuilderError, match="requires a Collection"):
        list(fetcher.fetch_sources(src))


# ---------------------------------------------------------------------------
# Finding 2: REGISTRY routing.
# ---------------------------------------------------------------------------


def test_fetcher_routes_registered_format_via_registry(monkeypatch, tmp_path: Path) -> None:
    """A ``source_format`` in ``REGISTRY.formats()`` routes through the builder.

    Pin for Finding 2: the fetcher must check ``REGISTRY`` BEFORE
    falling back to ``format_dispatch.dispatch``. We register a fake
    builder under the ``x-fake`` format, monkeypatch the REGISTRY's
    ``formats()`` / ``resolve()`` to surface it, and assert the
    builder's output becomes the body.
    """
    from lies.builders.base import REGISTRY

    seen = {"called": False}

    class _FakeBuilder:
        def build(self, workspace: Path, *, collection):
            seen["called"] = True
            from lies.scrapers.base import ParsedDoc

            return [
                ParsedDoc(
                    path="built.md",
                    content=b"# builder output\n",
                    source_sha256="cc" * 32,
                    source_format="markdown",
                )
            ]

    formats = set(REGISTRY.formats()) | {"x-fake"}

    monkeypatch.setattr(REGISTRY, "formats", lambda: formats)
    monkeypatch.setattr(REGISTRY, "resolve", lambda fmt: _FakeBuilder())

    fake = _FakeScraper(
        [
            ParsedDoc(
                path="page.x-fake",
                content=b"<fake>bytes</fake>",
                source_sha256="aa" * 32,
                source_format="x-fake",
            )
        ]
    )
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )

    src = tmp_path / "page.x-fake"
    src.write_bytes(b"placeholder")
    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    items = list(fetcher.fetch_sources(src))

    assert seen["called"] is True, "REGISTRY builder must be invoked for registered format"
    assert items[0].body == "# builder output\n"


def test_fetcher_markdown_skips_registry(monkeypatch, tmp_path: Path) -> None:
    """``source_format=markdown`` short-circuits REGISTRY (PassThrough is a no-op).

    Mirrors ``normalize.py:83``'s ``and doc.source_format != "markdown"``
    guard. The base ``PassThroughBuilder`` would re-decode bytes from
    a non-existent ``source.md`` file in a temp workspace; the
    dispatch pass-through keeps the bytes intact.
    """
    fake = _FakeScraper(
        [
            ParsedDoc(
                path="page.md",
                content=b"# direct markdown\n",
                source_sha256="aa" * 32,
                source_format="markdown",
            )
        ]
    )
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )
    src = tmp_path / "page.md"
    src.write_text("# hello\n")
    fetcher = ScraperFetcher(library=None)  # type: ignore[arg-type]
    items = list(fetcher.fetch_sources(src))
    assert items[0].body == "# direct markdown\n"


def test_fetcher_passes_collection_to_registry_builder(monkeypatch, tmp_path: Path) -> None:
    """The ``Collection`` is threaded through to the REGISTRY builder.

    Builders like ``sphinx`` / ``liquid`` / ``bespoke`` read
    ``collection.config`` for their include/exclude/rename config.
    Without the Collection, those builders raise
    ``AttributeError`` on the dataclass.
    """
    from lies.builders.base import REGISTRY

    seen_collection = {"value": None}

    class _FakeBuilder:
        def build(self, workspace: Path, *, collection):
            seen_collection["value"] = collection
            from lies.scrapers.base import ParsedDoc

            return [
                ParsedDoc(
                    path="b.md",
                    content=b"# from builder\n",
                    source_sha256="dd" * 32,
                    source_format="markdown",
                )
            ]

    formats = set(REGISTRY.formats()) | {"x-coll-aware"}
    monkeypatch.setattr(REGISTRY, "formats", lambda: formats)
    monkeypatch.setattr(REGISTRY, "resolve", lambda fmt: _FakeBuilder())

    from lies.collections.record import Collection
    from datetime import datetime

    coll = Collection(
        name="x",
        path=tmp_path,
        source="https://example.com/x",
        tags=[],
        scraper_cmd=None,
        doc_path=None,
        mapper_model=None,
        language=None,
        version="1",
        created_at=datetime.fromisoformat("2026-01-01T00:00:00"),
        updated_at=datetime.fromisoformat("2026-01-01T00:00:00"),
        config={"k": "v"},
    )

    fake = _FakeScraper(
        [
            ParsedDoc(
                path="page.x-coll-aware",
                content=b"raw",
                source_sha256="ee" * 32,
                source_format="x-coll-aware",
            )
        ]
    )
    monkeypatch.setattr(
        "lies.scrapers.base.pick_scraper",
        lambda source: fake,
    )

    src = tmp_path / "page.x-coll-aware"
    src.write_bytes(b"placeholder")
    fetcher = ScraperFetcher(library=None, collection=coll)  # type: ignore[arg-type]
    items = list(fetcher.fetch_sources(src))

    assert seen_collection["value"] is coll
    assert items[0].body == "# from builder\n"
