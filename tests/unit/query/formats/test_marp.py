import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from lies.query.formats.marp import render_marp


@pytest.fixture
def fake_marp_on_path() -> str:
    """Pretend marp is on PATH; tests inject the subprocess call."""
    return "/usr/local/bin/marp"


def test_render_marp_happy_path(
    tmp_path: Path, fake_marp_on_path: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = "---\nmarp: true\n---\n\n# S1\n"
    with patch("shutil.which", return_value=fake_marp_on_path):
        with patch(
            "lies.query.formats.marp.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ) as run_mock:
            html_path = render_marp(body, output_dir=tmp_path)

    assert html_path.suffix == ".html"
    assert html_path.parent == tmp_path
    body_path = html_path.with_suffix(".md")
    assert body_path.exists()
    assert body_path.read_text(encoding="utf-8") == body

    # Verify the subprocess call shape.
    args, kwargs = run_mock.call_args
    cmd = args[0]
    assert cmd[0] == "marp"
    assert cmd[1] == "--html"
    assert cmd[2] == "--output"
    assert cmd[3] == str(html_path)
    assert cmd[4] == str(body_path)
    assert kwargs.get("check") is True


def test_render_marp_fallback_when_marp_missing(
    tmp_path: Path,
) -> None:
    body = "---\nmarp: true\n---\n\n# S1\n"
    with patch("shutil.which", return_value=None):
        md_path = render_marp(body, output_dir=tmp_path)

    assert md_path.suffix == ".md"
    assert md_path.exists()
    assert md_path.read_text(encoding="utf-8") == body
    # No HTML sibling written.
    assert not md_path.with_suffix(".html").exists()


def test_render_marp_subprocess_failure_writes_stderr_sidecar(
    tmp_path: Path, fake_marp_on_path: str
) -> None:
    body = "---\nmarp: true\n---\n\n# S1\n"
    with patch("shutil.which", return_value=fake_marp_on_path):
        with patch(
            "lies.query.formats.marp.subprocess.run",
            side_effect=subprocess.CalledProcessError(
                returncode=1,
                cmd=["marp"],
                stderr="marp: theme not found",
            ),
        ):
            md_path = render_marp(body, output_dir=tmp_path)

    # Returns the markdown path (fallback), not HTML.
    assert md_path.suffix == ".md"
    assert md_path.exists()
    # Stderr sidecar exists at <md_path>.stderr.
    stderr_path = Path(str(md_path) + ".stderr")
    assert stderr_path.exists()
    assert "marp: theme not found" in stderr_path.read_text(encoding="utf-8")


def test_render_marp_creates_output_dir(tmp_path: Path) -> None:
    body = "---\nmarp: true\n---\n\n# S1\n"
    output_dir = tmp_path / "lies-cache" / "queries"
    assert not output_dir.exists()

    with patch("shutil.which", return_value=None):
        md_path = render_marp(body, output_dir=output_dir)

    assert output_dir.exists()
    assert md_path.parent == output_dir


def test_render_marp_timestamp_unique(tmp_path: Path, fake_marp_on_path: str) -> None:
    body = "---\nmarp: true\n---\n\n# S1\n"
    with patch("shutil.which", return_value=fake_marp_on_path):
        with patch(
            "lies.query.formats.marp.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ):
            path_a = render_marp(body, output_dir=tmp_path)
            path_b = render_marp(body, output_dir=tmp_path)

    # Two calls produce two distinct paths (timestamps differ).
    assert path_a != path_b
