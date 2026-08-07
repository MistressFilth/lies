"""Integration: registry survives across process boundaries."""

# ruff: noqa: I001  Import order is load-bearing: ``WikiMemoryService`` is imported first so the
# lies.memory package finishes loading before ``lies.collections.registry`` triggers the
# package's __init__; otherwise the lies.collections -> lies.memory chain races the
# lies.memory -> lies.collections reentry.
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path, PurePosixPath

from lies.memory.service import WikiMemoryService  # noqa: F401 - referenced from child_script
from lies.collections.registry import Registry
from lies.memory.models import WikiCollectionRef
from lies.wiki.wiki import Wiki  # noqa: F401 - referenced from child_script
from tests.conftest import make_wiki


def test_register_survives_subprocess_boundary(tmp_path: Path, child_env) -> None:
    wiki = make_wiki(name="crossproc", data_root=tmp_path / "wiki")
    wiki.registry_path.parent.mkdir(parents=True, exist_ok=True)
    wiki.collections_dir.mkdir(parents=True, exist_ok=True)
    (wiki.collections_dir / "subproc.yaml").write_text("name: subproc\n", encoding="utf-8")
    Registry.save(
        wiki,
        Registry(
            collections={
                "subproc": WikiCollectionRef(
                    collection_id="subproc",
                    root=PurePosixPath("/raw/subproc"),
                    qmd_collection="subproc",
                    schema_path=PurePosixPath(str(wiki.schema_path)),
                )
            }
        ),
    )

    # The fixture's autouse _isolated_xdg redirects XDG_* to tmp_path;
    # propagate the same env vars to the child so it sees the same wiki.
    env = child_env
    child_script = textwrap.dedent(
        f"""
        import sys
        from pathlib import Path, PurePosixPath
        from lies.memory.service import WikiMemoryService
        from lies.wiki.wiki import Wiki

        wiki = Wiki(
            name='crossproc',
            data_root=Path({str(wiki.data_root)!r}),
            config_root=Path({str(wiki.config_root)!r}),
            cache_root=Path({str(wiki.cache_root)!r}),
            state_root=Path({str(wiki.state_root)!r}),
            runtime_root=Path({str(wiki.runtime_root)!r}),
        )
        svc = WikiMemoryService(wiki=wiki)
        if not svc.is_registered('subproc'):
            sys.exit(1)
        print('OK')
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", child_script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"child failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    assert "OK" in result.stdout
