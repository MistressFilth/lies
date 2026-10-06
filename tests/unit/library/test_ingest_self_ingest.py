"""A self-ingest is a structural no-op, and an unmanaged mirror is
reported as a conflict it is not.

Both defects come from one place. ``_process_item`` computes
``target = coll.dir / f"{slug}.md"`` and, when the target already
exists, asks one two-state question about it:

    if existing_hash and existing_hash == item.source_hash:  # skip
    ...                                                     # quarantine

The domain has three states, not two. A mirror the pipeline never
wrote carries *no* ``source_hash`` at all, and the guard above falls
through to the quarantine branch that means "the mirror disagrees with
the source". The reason it writes is the tell::

    mirror-collision:page:existing-!=new-55d67183

``existing-`` with nothing after it is not a disagreement. It is the
absence of a value, described as a disagreement.

Measured, sandboxed, ``--force`` absent (``tools/probe_self_ingest.py``)::

    NORMAL ingest, run 1  created=1  errors=0
    NORMAL ingest, run 2  skipped=1  errors=0
    SELF   ingest, run 1  errors=1  QUARANTINE mirror-collision:page:existing-!=new-55d67183
    SELF   ingest, run 2  errors=1  QUARANTINE mirror-collision:page:existing-!=new-55d67183

The normal shape converges, which is the whole point of the
idempotency contract. The self shape never does, and never writes a
byte.

A self-ingest is ``lies ingest --batch`` pointed at a live collection
directory, or ``--source`` pointed at a page inside one. The walk then
enumerates the mirrors and asks each one whether its own source has
changed. There is no answer to give that is not circular: the only
reachable outcomes are "skip everything" and "rewrite everything", and
today it is neither — it is a run that reports an error for every page
it finds and changes nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lies.library.errors import SelfIngestRefused
from lies.library.fetcher import ScraperFetcher
from lies.library.ingest import run_batch_ingest, run_source_ingest
from lies.library.paths import Library

# Long enough to clear ``should_skip_content``. The point of these tests
# is the idempotency path, and a thin-content quarantine would fire
# first and make every assertion here about the wrong thing.
_BODY = (
    "# A page\n\n"
    "Padding prose that carries the body comfortably past the thin-content\n"
    "threshold, so a quarantine here is a statement about ingest and not\n"
    "about how much text happens to be in the file.\n\n"
    "A second paragraph, for the same reason, and to give the content gate\n"
    "no excuse to fire before the hash comparison is ever reached.\n"
)


@pytest.fixture
def lib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Library:
    from lies import xdg

    monkeypatch.setattr(xdg, "data_home", lambda: tmp_path)
    Library.open.cache_clear()
    return Library.open()


def _collection(lib: Library, name: str) -> Path:
    """An existing collection dir with a config, as a real run would find."""
    coll = lib.collections_root / name
    coll.mkdir(parents=True, exist_ok=True)
    (coll / "config.yaml").write_text(f"name: {name}\nsource: local:.\n")
    return coll


def _ingest(lib: Library, collection: str, source: Path):
    return run_batch_ingest(lib, collection, source, fetcher=ScraperFetcher(library=lib))


@pytest.fixture
def no_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the git commit at the end of ``_finalize``.

    Every assertion in this module is about the *decision* — refused,
    quarantined with which reason, skipped or created — and all of those
    are settled before ``_finalize`` runs. What follows is a real
    ``git`` subprocess, which is the only thing these unit tests pay
    for beyond the behaviour under test, and at 0.26-0.41s each it
    breached the 0.15s per-test budget on three of them.

    Stubbed at ``LibraryWriter.commit``, not at ``run_batch_ingest``, so
    the walk, the filters, ``_process_item`` and ``write_mirror`` all
    still run for real.
    """
    monkeypatch.setattr(
        "lies.library.ingest.LibraryWriter.commit",
        lambda self, *a, **k: "0" * 40,
    )


# ---------------------------------------------------------------------
# Defect 1 — a self-ingest is refused instead of run into a no-op
# ---------------------------------------------------------------------


def test_a_batch_ingest_of_a_collection_dir_is_refused(lib: Library) -> None:
    """``--batch <collection_dir>`` is refused before any page is read.

    Not quarantined per page and not silently skipped: the whole run is
    a caller error, and the caller is the only thing that can fix it.
    """
    coll = _collection(lib, "selfcoll")
    (coll / "page.md").write_text(_BODY)

    with pytest.raises(SelfIngestRefused) as excinfo:
        _ingest(lib, "selfcoll", coll)

    message = str(excinfo.value)
    assert str(coll) in message, "the refusal must name the directory that was passed"
    assert "selfcoll" in message, "the refusal must name the collection it resolved to"


def test_a_batch_ingest_of_a_subdirectory_of_a_collection_is_refused(
    lib: Library,
) -> None:
    """A subdirectory is the same defect, not a smaller one.

    ``derive_nested_slug`` mirrors the relative path, so
    ``--batch <coll>/guides`` still lands every page on top of itself
    at ``<coll>/guides/<slug>.md``. Checking only for equality would
    let this through and reproduce the identical no-op.
    """
    coll = _collection(lib, "selfcoll")
    guides = coll / "guides"
    guides.mkdir()
    (guides / "page.md").write_text(_BODY)

    with pytest.raises(SelfIngestRefused):
        _ingest(lib, "selfcoll", guides)


def test_a_source_ingest_of_a_page_inside_a_collection_is_refused(lib: Library) -> None:
    """``--source`` into a collection page hits the same collision.

    One page, one target, the same path. Guarding only the batch entry
    point would leave the single-source spelling of the same mistake
    unguarded.
    """
    coll = _collection(lib, "selfcoll")
    (coll / "page.md").write_text(_BODY)

    with pytest.raises(SelfIngestRefused):
        run_source_ingest(lib, "selfcoll", coll / "page.md", fetcher=ScraperFetcher(library=lib))


def test_the_refusal_happens_before_anything_is_written(lib: Library) -> None:
    """A refused run leaves the tree exactly as it found it.

    The per-page path this replaces *did* write: it created a poison
    copy of every page and a ``.reason`` sidecar, so refusing late would
    leave the artifacts the refusal is meant to prevent.
    """
    coll = _collection(lib, "selfcoll")
    (coll / "page.md").write_text(_BODY)
    before = sorted(p.name for p in lib.git_root.rglob("*"))

    with pytest.raises(SelfIngestRefused):
        _ingest(lib, "selfcoll", coll)

    assert sorted(p.name for p in lib.git_root.rglob("*")) == before


def test_a_normal_ingest_of_a_sibling_directory_is_still_allowed(
    lib: Library,
    no_commit: None,
) -> None:
    """The refusal keys on containment, not on the word "collection".

    A source tree that merely sits next to the library is the ordinary
    case; refusing it would break every real ingest.
    """
    _collection(lib, "normalcoll")
    source = lib.git_root.parent / "elsewhere"
    source.mkdir(parents=True, exist_ok=True)
    (source / "page.md").write_text(_BODY)

    result = _ingest(lib, "normalcoll", source)

    assert result.created == 1, f"a normal ingest was refused: {result}"
    assert result.errors == 0


def test_a_normal_ingest_still_converges_on_a_second_run(
    lib: Library,
    no_commit: None,
) -> None:
    """The fix leaves the idempotency contract intact.

    Without this, a guard that refused too much would still pass every
    refusal test above while making ingest impossible.
    """
    _collection(lib, "normalcoll")
    source = lib.git_root.parent / "elsewhere"
    source.mkdir(parents=True, exist_ok=True)
    (source / "page.md").write_text(_BODY)

    first = _ingest(lib, "normalcoll", source)
    second = _ingest(lib, "normalcoll", source)

    assert (first.created, first.errors) == (1, 0)
    assert (second.skipped, second.errors) == (1, 0), (
        f"an unchanged source must skip, not rewrite; got {second}"
    )


# ---------------------------------------------------------------------
# Defect 2 — an unmanaged mirror is named for what it is
# ---------------------------------------------------------------------


def test_a_mirror_with_no_source_hash_is_not_reported_as_a_conflict(
    lib: Library,
) -> None:
    """The three-state question, stated as the property.

    A page sitting where a mirror would sit, carrying no frontmatter,
    is a page the pipeline never wrote. Reporting
    ``existing-!=new-<hash>`` tells the operator their mirror disagrees
    with the source when in fact their mirror was never recorded at all
    — and an empty left-hand side is the proof, printed in the message.
    """
    coll = _collection(lib, "handwritten")
    (coll / "page.md").write_text(_BODY)
    source = lib.git_root.parent / "elsewhere"
    source.mkdir(parents=True, exist_ok=True)
    (source / "page.md").write_text(_BODY)

    result = _ingest(lib, "handwritten", source)

    reasons = [reason for _rel, reason in result.quarantine_records]
    assert reasons, f"the unmanaged page must still be quarantined; got {result}"
    assert not any(r.startswith("mirror-collision:") for r in reasons), (
        f"an unmanaged mirror is not a collision: {reasons}"
    )
    assert not any("existing-!=" in r for r in reasons), (
        f"an empty existing hash is not a disagreement: {reasons}"
    )
    assert any("no-source-hash" in r for r in reasons), (
        f"the reason must name the absent hash: {reasons}"
    )


def test_an_unmanaged_mirror_is_still_preserved(lib: Library) -> None:
    """Fail-loud, not fail-destructive.

    The page is quarantined, so it is preserved, and the operator's
    content is not overwritten by a hash decision they did not make.
    """
    coll = _collection(lib, "handwritten")
    (coll / "page.md").write_text(_BODY)
    source = lib.git_root.parent / "elsewhere"
    source.mkdir(parents=True, exist_ok=True)
    (source / "page.md").write_text(_BODY)

    _ingest(lib, "handwritten", source)

    assert (coll / "page.md").read_text() == _BODY, "the operator's page was overwritten"


def test_a_genuine_hash_mismatch_still_says_mirror_collision(lib: Library) -> None:
    """The real conflict keeps its real name.

    A three-state test that made the third state honest must not have
    widened the second: a mirror carrying a *different* hash is a
    disagreement, and the operator is told so.
    """
    coll = _collection(lib, "conflicted")
    (coll / "page.md").write_text(
        '---\ntitle: "Page"\nsource_url: ""\nsource_path: "page.md"\n'
        "source_hash: 0000000000000000000000000000000000000000000000000000000000000000\n"
        "fetched_via: local\ningested_at: 2026-10-04\n---\n\n" + _BODY
    )
    source = lib.git_root.parent / "elsewhere"
    source.mkdir(parents=True, exist_ok=True)
    (source / "page.md").write_text(_BODY)

    result = _ingest(lib, "conflicted", source)

    reasons = [reason for _rel, reason in result.quarantine_records]
    assert any(r.startswith("mirror-collision:") for r in reasons), (
        f"a genuine mismatch must keep the collision reason: {reasons}"
    )
    assert not any("no-source-hash" in r for r in reasons), (
        f"a mirror with a hash is managed, not unmanaged: {reasons}"
    )


def test_an_unchanged_source_still_skips_against_a_managed_mirror(
    lib: Library,
    no_commit: None,
) -> None:
    """And the state the contract was written for is untouched."""
    _collection(lib, "normalcoll")
    source = lib.git_root.parent / "elsewhere"
    source.mkdir(parents=True, exist_ok=True)
    (source / "page.md").write_text(_BODY)

    _ingest(lib, "normalcoll", source)
    result = _ingest(lib, "normalcoll", source)

    assert result.quarantine_records == [], f"an unchanged source must not quarantine: {result}"
    assert result.skipped == 1


# ---------------------------------------------------------------------
# The CLI surface — a refusal an operator can read
# ---------------------------------------------------------------------


def test_the_cli_renders_the_refusal_without_a_traceback(lib: Library, tmp_path: Path) -> None:
    """Exit 2 and one ``error:`` line, like the command's other refusals.

    Without this the typed error escapes ``ingest`` and Rich prints a
    twelve-line traceback: louder, and saying less. The operator is
    told which library line they tripped rather than what to pass
    instead. Driven through the real Typer app so the rendering and the
    exit code are both observed, not assumed.
    """
    from typer.testing import CliRunner

    from lies.cli import app

    coll = _collection(lib, "selfcoll")
    (coll / "page.md").write_text(_BODY)

    result = CliRunner().invoke(app, ["ingest", "--batch", str(coll)])

    assert result.exit_code == 2, f"expected the refusal exit code; got {result.output}"
    assert "error:" in result.output, (
        f"the refusal must use the command's own prefix: {result.output}"
    )
    assert "selfcoll" in result.output, "the refusal must name the collection"
    assert "Traceback" not in result.output, (
        f"a caller error must not render as a traceback: {result.output}"
    )
