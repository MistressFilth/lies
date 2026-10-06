"""Phase 1 + Phase 2 probe: why does a self-ingest fail, and what does a
normal ingest do with the identical file?

Runs both shapes in one process against one sandbox root and prints the
``BatchIngestResult`` fields the CLI summary does not carry -- the
quarantine reason is the whole point, and ``created=0 updated=0
skipped=0 errors=1`` alone does not say which branch fired.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path


def _sandbox() -> Path:
    root = Path(tempfile.mkdtemp(prefix="selfingest."))
    (root / "cache").mkdir()
    (root / "config").mkdir()
    (root / "data" / "lies" / "library" / "collections").mkdir(parents=True)
    os.environ["XDG_DATA_HOME"] = str(root / "data")
    os.environ["XDG_CACHE_HOME"] = str(root / "cache")
    os.environ["XDG_CONFIG_HOME"] = str(root / "config")
    os.environ["NO_COLOR"] = "1"
    return root


BODY = """# A page

This body is deliberately long enough that the thin-content quarantine
guard lets it through, so that a failure here is a failure of the
idempotency contract rather than of a content-length filter that has
nothing to do with what is being tested.

Second paragraph, also padding, also present only to give the content
filter no excuse to fire before the hash comparison is ever reached.
"""

COLL_CONFIG = "name: {name}\nsource: local:.\n"


def _collection(root: Path, name: str) -> Path:
    coll = root / "data" / "lies" / "library" / "collections" / name
    coll.mkdir(parents=True, exist_ok=True)
    (coll / "config.yaml").write_text(COLL_CONFIG.format(name=name))
    return coll


def _run(label: str, source_dir: Path, collection: str, root: Path) -> None:
    from lies.library.fetcher import ScraperFetcher
    from lies.library.ingest import run_batch_ingest
    from lies.library.paths import Library

    library = Library.open()
    fetcher = ScraperFetcher(library)
    result = run_batch_ingest(library, collection, source_dir, fetcher=fetcher)

    print(f"--- {label}")
    print(f"    source dir : {source_dir}")
    print(f"    collection : {collection}")
    for key, value in asdict(result).items():
        if key == "mirror_paths":
            print(f"    {key}: {[Path(p).name for p in value]}")
        elif key == "quarantine_records":
            continue
        else:
            print(f"    {key}: {value!r}")
    for _rel, reason in result.quarantine_records:
        print(f"    QUARANTINE: {reason}")
    print(
        f"    collection dir now holds: {sorted(p.name for p in (root / 'data' / 'lies' / 'library' / 'collections' / collection).iterdir())}"
    )
    print()


def main() -> int:
    root = _sandbox()
    print(f"sandbox: {root}\n")

    # Phase 2: the working example. Same bytes, same code, but the source
    # lives outside the collection dir so source != mirror.
    _collection(root, "normalcoll")
    normal_source = root / "outside"
    normal_source.mkdir(parents=True, exist_ok=True)
    (normal_source / "page.md").write_text(BODY)
    _run("NORMAL ingest (source outside the collection dir)", normal_source, "normalcoll", root)
    _run("NORMAL ingest, second run, same tree", normal_source, "normalcoll", root)

    # The failing shape: --batch pointed at the collection dir itself.
    self_coll = _collection(root, "selfcoll")
    (self_coll / "page.md").write_text(BODY)
    _run("SELF ingest (--batch pointed at the collection dir)", self_coll, "selfcoll", root)
    _run("SELF ingest, second run, same tree", self_coll, "selfcoll", root)

    return 0


if __name__ == "__main__":
    sys.exit(main())
