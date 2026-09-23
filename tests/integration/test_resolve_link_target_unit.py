"""Unit tests for ``_resolve_link_target`` (orchestrator internal helper)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from lies.orchestrator import _resolve_link_target


def _seed_wiki(root: Path) -> Path:
    """Bootstrap a wiki with one source page and one target under ``concepts/``."""
    root = root / "wiki"
    for sub in ("wiki", ".lies", "raw"):
        (root / sub).mkdir(parents=True)
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "index.md").write_text("# Index\n", encoding="utf-8")
    (root / ".lies" / "schema.md").write_text("## Page types\n- concept\n", encoding="utf-8")
    subprocess.run(["git", "init", "--initial-branch=main", str(root)], check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, check=True)
    return root


def test_repository_style_link_resolves_to_existing_target(tmp_path: Path) -> None:
    """The repository's normal ``concepts/<name>.md`` style must resolve
    correctly. Regression for the false-positive bug where the
    source-relative candidate ``concepts/concepts/beta.md`` (syntactically
    valid but non-existent) shadowed the correct wiki-root fallback
    ``concepts/beta.md``.
    """
    root = _seed_wiki(tmp_path)
    (root / "wiki" / "concepts" / "alpha.md").write_text(
        "---\ntitle: Alpha\ntype: concept\n---\n# Alpha\n\nSee [Beta](concepts/beta.md).\n",
        encoding="utf-8",
    )
    (root / "wiki" / "concepts" / "beta.md").write_text(
        "---\ntitle: Beta\ntype: concept\n---\n# Beta\n",
        encoding="utf-8",
    )
    resolved = _resolve_link_target("wiki/concepts/alpha.md", "concepts/beta.md", root)
    assert resolved == "wiki/concepts/beta.md"


def test_source_relative_link_resolves_when_target_in_same_dir(tmp_path: Path) -> None:
    root = _seed_wiki(tmp_path)
    (root / "wiki" / "concepts" / "alpha.md").write_text(
        "---\ntitle: Alpha\ntype: concept\n---\n# Alpha\n\nSee [Beta](beta.md).\n",
        encoding="utf-8",
    )
    (root / "wiki" / "concepts" / "beta.md").write_text(
        "---\ntitle: Beta\ntype: concept\n---\n# Beta\n",
        encoding="utf-8",
    )
    resolved = _resolve_link_target("wiki/concepts/alpha.md", "beta.md", root)
    assert resolved == "wiki/concepts/beta.md"


def test_returns_none_when_target_does_not_exist_anywhere(tmp_path: Path) -> None:
    """A link with no on-disk target must NOT silently resolve to a
    source-relative typo path. Without the existence check, a link
    like ``[X](concepts/x.md)`` from ``wiki/concepts/alpha.md`` would
    resolve to ``wiki/concepts/concepts/x.md`` even when neither that
    file nor any wiki-root fallback exists.
    """
    root = _seed_wiki(tmp_path)
    (root / "wiki" / "concepts" / "alpha.md").write_text(
        "---\ntitle: Alpha\ntype: concept\n---\n# Alpha\n",
        encoding="utf-8",
    )
    resolved = _resolve_link_target("wiki/concepts/alpha.md", "concepts/x.md", root)
    assert resolved is None


def test_returns_none_for_non_md_target(tmp_path: Path) -> None:
    root = _seed_wiki(tmp_path)
    (root / "wiki" / "concepts" / "alpha.md").write_text(
        "---\ntitle: Alpha\ntype: concept\n---\n# Alpha\n",
        encoding="utf-8",
    )
    (root / "wiki" / "concepts" / "beta.txt").write_text("not markdown\n", encoding="utf-8")
    resolved = _resolve_link_target("wiki/concepts/alpha.md", "beta.txt", root)
    assert resolved is None


def test_extract_local_md_links_skips_target_outside_wiki(tmp_path: Path) -> None:
    """A link that resolves to a sibling of ``wiki/`` (e.g. ``../docs/x.md``
    from inside ``wiki/concepts/a.md``) must NOT show up in the helper's
    output. Without the containment check, the resolved path
    ``docs/x.md`` would strip the ``wiki/`` prefix and collide with the
    real wiki page key ``wiki/concepts/x.md``, producing false
    orphan/missing_xref suppressions.

    Regression for the outside-wiki containment bug: links that escape
    the wiki directory via ``..`` traversal are not local wiki links.
    """
    from lies.orchestrator import _extract_local_md_links

    root = _seed_wiki(tmp_path)
    (root / "wiki" / "concepts" / "alpha.md").write_text(
        "---\ntitle: Alpha\ntype: concept\n---\n# Alpha\n\nSee [Outside](../../docs/escaped.md).\n",
        encoding="utf-8",
    )
    # Create the target outside the wiki directory but inside the repo.
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "escaped.md").write_text(
        "---\ntitle: Escaped\n---\n# Escaped\n",
        encoding="utf-8",
    )
    targets = _extract_local_md_links(
        (root / "wiki" / "concepts" / "alpha.md").read_text(encoding="utf-8"),
        "concepts/alpha.md",
        root,
    )
    assert "docs/escaped.md" not in targets, (
        f"helper must skip targets outside wiki/, got {targets!r}"
    )
    assert targets == set()
