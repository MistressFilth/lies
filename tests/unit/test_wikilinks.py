"""Unit tests for the wikilink extraction helper."""

from __future__ import annotations

import importlib
import sys
import warnings
from pathlib import Path

import pytest

from lies.wikilinks import (
    PageKey,  # noqa: F401  (re-exported from the public API)
    WikiLinkCorpusMissing,
    WikiLinkResolver,
    extract_wikilinks,
)


class TestExtractWikilinks:
    def test_bare(self) -> None:
        assert extract_wikilinks("see [[Page]] for details") == ["Page"]

    def test_alias_discarded(self) -> None:
        assert extract_wikilinks("see [[Page|display text]] here") == ["Page"]

    def test_heading_discarded(self) -> None:
        assert extract_wikilinks("see [[Page#Heading]] here") == ["Page"]

    def test_mixed_preserves_order(self) -> None:
        text = "[[First]] then [[Second|other]] then [[Third#Anchor]]"
        assert extract_wikilinks(text) == ["First", "Second", "Third"]

    def test_inline_code_skipped(self) -> None:
        text = "real [[Real]] but not `[[Fake]]`"
        assert extract_wikilinks(text) == ["Real"]

    def test_fenced_code_block_skipped(self) -> None:
        text = "real [[Real]]\n\n```\n[[Fake]]\n```\n"
        assert extract_wikilinks(text) == ["Real"]

    def test_tilde_fenced_code_block_skipped(self) -> None:
        text = "real [[Real]]\n\n~~~\n[[Fake]]\n~~~\n"
        assert extract_wikilinks(text) == ["Real"]

    def test_embed_not_captured(self) -> None:
        # ![[...]] is out of scope per the spec; not captured.
        assert extract_wikilinks("see ![[Embed]] here") == []

    def test_empty_target_skipped(self) -> None:
        # Regex requires non-empty target; [[]] produces nothing.
        assert extract_wikilinks("text [[]] more") == []

    def test_no_wikilinks(self) -> None:
        assert extract_wikilinks("just plain text, no links.") == []

    def test_md_extension_preserved_in_extraction(self) -> None:
        # Extension stripping happens in resolve(), not extract().
        assert extract_wikilinks("[[Page.md]]") == ["Page.md"]


def _write_page(root: Path, rel: str, body: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


class TestResolverBuildAndResolveDict:
    def test_single_page_filename_only(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_page(wiki, "alpha.md", "# Alpha\n")

        r = WikiLinkResolver.build((wiki,))
        assert r.resolve("Alpha") == (wiki / "alpha.md").resolve()
        assert r.resolve("alpha") == (wiki / "alpha.md").resolve()
        assert r.resolve("ALPHA") == (wiki / "alpha.md").resolve()

    def test_multiple_keys_title_and_aliases(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_page(
            wiki,
            "alpha.md",
            "---\ntitle: First\naliases: [uno, one]\nalias: primary\n---\n# body\n",
        )

        r = WikiLinkResolver.build((wiki,))
        path = (wiki / "alpha.md").resolve()
        for key in ("alpha", "first", "uno", "one", "primary"):
            assert r.resolve(key) == path, f"failed for key {key!r}"

    def test_trailing_md_stripped(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_page(wiki, "alpha.md", "# Alpha\n")

        r = WikiLinkResolver.build((wiki,))
        assert r.resolve("Alpha.md") == (wiki / "alpha.md").resolve()
        assert r.resolve("Alpha.markdown") == (wiki / "alpha.md").resolve()

    def test_collision_last_write_wins(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_page(wiki, "a/foo.md", "first\n")
        _write_page(wiki, "b/foo.md", "second\n")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            r = WikiLinkResolver.build((wiki,))
        assert any("foo" in str(w.message) for w in caught)
        # Last write wins; second foo.md overwrites first.
        assert r.resolve("foo") == (wiki / "b/foo.md").resolve()

    def test_empty_target_returns_none(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        r = WikiLinkResolver.build((wiki,))
        assert r.resolve("") is None
        assert r.resolve("   ") is None

    def test_unknown_target_returns_none(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_page(wiki, "alpha.md", "# Alpha\n")
        r = WikiLinkResolver.build((wiki,))
        assert r.resolve("Beta") is None

    def test_missing_root_skipped(self, tmp_path: Path) -> None:
        # wiki/ does not exist; raw/ does. raw/ alone is enough.
        raw = tmp_path / "raw"
        raw.mkdir()
        _write_page(raw, "alpha.md", "# Alpha\n")
        r = WikiLinkResolver.build((tmp_path / "wiki", raw))
        assert r.resolve("alpha") == (raw / "alpha.md").resolve()

    def test_both_roots_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(WikiLinkCorpusMissing):
            WikiLinkResolver.build((tmp_path / "wiki", tmp_path / "raw"))

    def test_bad_frontmatter_skipped_with_warning(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_page(wiki, "good.md", "# Good\n")
        _write_page(wiki, "bad.md", "---\n: bad yaml :\n---\n# body\n")

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            r = WikiLinkResolver.build((wiki,))
        # bad.md still indexed by stem; only its title/aliases keys are skipped.
        assert r.resolve("good") is not None
        assert r.resolve("bad") is not None
        assert any("bad.md" in str(w.message) for w in caught)

    def test_excluded_directories_skipped(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_page(wiki, "alpha.md", "# Alpha\n")
        _write_page(wiki, ".lies/skip.md", "# Skip\n")
        _write_page(wiki, ".git/skip.md", "# Skip\n")
        _write_page(wiki, "node_modules/skip.md", "# Skip\n")

        r = WikiLinkResolver.build((wiki,))
        assert r.resolve("alpha") is not None
        assert r.resolve("skip") is None

    def test_empty_corpus_resolves_all_none(self, tmp_path: Path) -> None:
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        r = WikiLinkResolver.build((wiki,))
        assert r.resolve("anything") is None

    def test_resolve_prefix_overlapping_keys_longest_match(self, tmp_path: Path) -> None:
        """Prefix-overlapping keys: longest basename wins the lookup.

        Two pages whose stems share a prefix (foo, foobar). Each key in
        the resolver's dict is, by construction, the full normalized
        lowercased basename — so a query for ``foobar`` matches only
        ``foobar.md`` and a query for ``foo`` matches only ``foo.md``.
        The collision in ``build()`` (when both pages share a stem)
        is unrelated.
        """
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_page(wiki, "foo.md", "# foo\n")
        _write_page(wiki, "foobar.md", "# foobar\n")

        r = WikiLinkResolver.build((wiki,))
        assert r.resolve("foobar") == (wiki / "foobar.md").resolve()
        assert r.resolve("foo") == (wiki / "foo.md").resolve()
        assert r.resolve("qux") is None


class TestResolverUsesAhoCorasick:
    """When ahocorasick_rs is importable, the resolver builds the automaton
    inside ``build()`` and exercises ``find_matches_as_strings`` on every
    ``resolve()`` call. Without the dep, ``_aho`` is ``None`` and the dict
    branch alone runs.

    The two tests below pin the contract:

    1. ``_aho`` is set when the dep imports cleanly.
    2. AC-backed ``resolve()`` returns the same Path as dict-only
       ``resolve()`` for every key — including prefix-overlapping keys
       (``foo`` + ``foobar``), where ``overlapping=True`` is required to
       surface the longer match. This is the same longest-wins guarantee
       the dict branch pins in :class:`TestResolverBuildAndResolveDict`.
    """

    def test_ac_is_built_when_import_succeeds(self, tmp_path: Path) -> None:
        try:
            import ahocorasick_rs  # noqa: F401
        except ImportError:
            pytest.skip("ahocorasick_rs not installed in this environment")

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_page(wiki, "alpha.md", "# Alpha\n")
        _write_page(wiki, "beta.md", "# Beta\n")

        r = WikiLinkResolver.build((wiki,))
        assert r._aho is not None, (
            "ahocorasick_rs is importable; WikiLinkResolver.build() should build "
            "the automaton and store it on _aho"
        )
        # Sanity: the AC path still resolves correctly.
        assert r.resolve("alpha") == (wiki / "alpha.md").resolve()
        assert r.resolve("Beta") == (wiki / "beta.md").resolve()

    def test_aho_backed_results_match_dict(self, tmp_path: Path) -> None:
        """AC-backed ``resolve()`` returns the same Path as dict-only for every
        key — including prefix-overlapping keys where ``overlapping=True`` is
        required to surface the longer match.
        """
        try:
            import ahocorasick_rs  # noqa: F401
        except ImportError:
            pytest.skip("ahocorasick_rs not installed in this environment")

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_page(
            wiki,
            "alpha.md",
            "---\ntitle: First\naliases: [uno]\n---\n# body\n",
        )
        _write_page(wiki, "beta.md", "# Beta\n")
        _write_page(wiki, "foo.md", "# Foo\n")
        _write_page(wiki, "foobar.md", "# Foobar\n")

        r = WikiLinkResolver.build((wiki,))
        assert r._aho is not None

        # Build a sibling resolver and force the dict branch by clearing _aho.
        dict_only = WikiLinkResolver.build((wiki,))
        dict_only._aho = None

        for key in ("alpha", "first", "uno", "beta", "foo", "foobar", "qux"):
            assert r.resolve(key) == dict_only.resolve(key), (
                f"AC-backed resolve({key!r}) diverged from dict-only"
            )


class TestResolverImportFallback:
    """When ``ahocorasick_rs`` cannot be imported, ``build()`` succeeds and
    ``resolve()`` still returns correct results via the dict fallback.

    Patches ``sys.modules`` to force the ``import ahocorasick_rs`` inside
    ``build()`` to raise ``ImportError``. The patch is scoped via the
    ``monkeypatch`` fixture and undone automatically on teardown; we
    additionally drop any cached module reference so a follow-up test
    sees the real dep again.
    """

    def test_dict_fallback_when_import_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Force the import inside build() to fail.
        monkeypatch.setitem(sys.modules, "ahocorasick_rs", None)

        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_page(wiki, "alpha.md", "# Alpha\n")
        _write_page(wiki, "beta.md", "# Beta\n")
        _write_page(wiki, "foo.md", "# Foo\n")
        _write_page(wiki, "foobar.md", "# Foobar\n")

        r = WikiLinkResolver.build((wiki,))
        assert r._aho is None, "ahocorasick_rs is forced to fail; resolver._aho must be None"

        # Dict branch alone: same longest-wins guarantees the AC path has.
        assert r.resolve("alpha") == (wiki / "alpha.md").resolve()
        assert r.resolve("Beta") == (wiki / "beta.md").resolve()
        assert r.resolve("foo") == (wiki / "foo.md").resolve()
        assert r.resolve("foobar") == (wiki / "foobar.md").resolve()
        assert r.resolve("qux") is None

    def test_dict_fallback_restored_after_force_fail(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """After a forced-failure test, the dep should be importable again.

        ``monkeypatch`` restores ``sys.modules`` on teardown, so a fresh
        ``import ahocorasick_rs`` succeeds. This pins the contract that
        the patched-import test does not leak module-level state.
        """
        # Pre-condition: the dep is importable (skip otherwise).
        try:
            import ahocorasick_rs
        except ImportError:
            pytest.skip("ahocorasick_rs not installed in this environment")

        # Simulate one full force-fail cycle without leaving a stale binding.
        monkeypatch.setitem(sys.modules, "ahocorasick_rs", None)
        # Drop any cached module so the next import re-resolves.
        sys.modules.pop("ahocorasick_rs", None)
        # Reload lies.wikilinks in case the import was bound at module load.
        importlib.reload(sys.modules["lies.wikilinks"])

        # Restore: undo the monkeypatch and re-import.
        monkeypatch.undo()
        importlib.reload(sys.modules["lies.wikilinks"])

        # Post-condition: the dep is importable again.
        import ahocorasick_rs  # noqa: F401

        # And the AC path is alive again.
        wiki = tmp_path / "wiki"
        wiki.mkdir()
        _write_page(wiki, "alpha.md", "# Alpha\n")
        r = WikiLinkResolver.build((wiki,))
        assert r._aho is not None
