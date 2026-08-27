"""End-to-end bootstrap coverage for ingest + ingest_source.

Gated on ``INTEGRATION=1`` so a default ``make test`` run skips these
(network-adjacent, real-filesystem) tests. The CLI test drives
``lies ingest`` end-to-end against a local HTTP fixture serving an
``llms.txt``-shaped body; the MCP test drives ``ingest_source`` (the
equivalent in the FastMCP server) and asserts the same on-disk
artifacts land under the XDG-routed config root.

Run: ``INTEGRATION=1 pytest tests/integration/test_bootstrap_ingest.py -v``
"""

from __future__ import annotations

import http.server
import os
import socketserver
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lies.cli import app
from lies.wiki.wiki import Wiki

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("INTEGRATION") != "1",
        reason="set INTEGRATION=1 to run integration tests",
    ),
]


LLMS_TXT_BODY = """# Pydantic AI

## Agents

- [Run an agent](https://docs.pydantic.dev/ai/agents/)
- [Dependencies](https://docs.pydantic.dev/ai/dependencies/)
"""


class _Handler(http.server.BaseHTTPRequestHandler):
    body = LLMS_TXT_BODY.encode("utf-8")

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args: object, **kwargs: object) -> None:
        return  # silence


@pytest.fixture
def http_url() -> str:
    """Spin up a single-shot HTTP server on an OS-assigned localhost port.

    Yields the URL pointing at the served body and tears the server
    down at test end. The brief mandates ``127.0.0.1`` with port 0 so
    the OS picks a free port and parallel runs do not collide.

    The URL path is ``/alpha`` (rather than ``/llms.txt``) on purpose:
    ``Orchestrator.run_ingest`` derives the collection name from
    ``Path(source).stem``. If the path basename is ``llms.txt`` the
    derived stem is ``llms``, which would not match the ``collection``
    argument the MCP test bootstraps and the ``sync_collection`` call
    would raise ``CollectionNotFound``. Serving under ``/alpha`` keeps
    the WebScraper happy (it walks parent directories to find an
    ``llms.txt``-shaped candidate and our handler responds with the
    llms-shaped body) while deriving the right collection stem.
    """
    with socketserver.TCPServer(("127.0.0.1", 0), _Handler) as httpd:
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{port}/alpha"
        finally:
            httpd.shutdown()
            thread.join(timeout=2)


@pytest.mark.integration
def test_cli_ingest_end_to_end(
    http_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``lies ingest <c> --source <http-url>`` bootstraps wiki + YAML end-to-end.

    All XDG roots are monkeypatched to ``tmp_path`` so the resolved
    wiki lives entirely under the per-test scratch dir. The CLI must
    succeed, the wiki's data root must contain ``raw/<c>/`` (created
    by the SCRAPE stage as soon as it mkdir's the per-collection raw
    dir), and the collection YAML must land at the XDG-derived config
    path (``$XDG_CONFIG_HOME/lies/<name>/collections/<c>.yaml``).
    """
    name = "integ-cli"
    monkeypatch.setenv("LIES_WIKI_NAME", name)
    monkeypatch.setattr("lies.cli.xdg.data_home", lambda: tmp_path)
    monkeypatch.setattr("lies.cli.xdg.config_home", lambda: tmp_path)
    monkeypatch.setattr("lies.cli.xdg.cache_home", lambda: tmp_path)
    monkeypatch.setattr("lies.cli.xdg.state_home", lambda: tmp_path)
    monkeypatch.setattr("lies.wiki.wiki.xdg.runtime_dir_for", lambda n: tmp_path / "run" / n)

    runner = CliRunner()
    result = runner.invoke(app, ["ingest", "alpha", "--source", http_url])
    assert result.exit_code == 0, result.stdout

    wiki_root = Wiki.data_root_for(name)
    assert (wiki_root / "raw" / "alpha").exists()
    # ``wiki.collections_dir`` resolves to ``xdg.config_home() / "lies" / <name> /
    # "collections"``. With ``xdg.config_home`` monkeypatched to ``tmp_path``,
    # the XDG root segment is the path itself (the ``LIES_DATA_SUBDIR``
    # segment is appended below it), so the YAML lives at
    # ``tmp_path / "lies" / <name> / "collections"``.
    cfg_root = tmp_path / "lies" / name / "collections"
    assert (cfg_root / "alpha.yaml").exists()


@pytest.mark.integration
def test_mcp_ingest_source_end_to_end(
    http_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ingest_source(source, collection, name)`` bootstraps via the MCP path.

    Drives the FastMCP server's ``ingest_source`` tool directly (no
    client socket layer). Like the CLI test, the wiki is created from
    scratch, the YAML is written at the XDG-derived config path, and
    the tool returns a non-empty string confirming the orchestrator
    pipeline ran.

    ``ANTHROPIC_API_KEY`` is set so the Anthropic provider constructs
    during ``Orchestrator(wiki)`` — the actual API is never called by
    ``run_ingest`` (which delegates to ``sync_collection`` and skips
    the LLM path), but pydantic-ai demands the key at agent-build
    time.
    """
    name = "integ-mcp"
    monkeypatch.setenv("LIES_WIKI_NAME", name)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setattr("lies.cli.xdg.data_home", lambda: tmp_path)
    monkeypatch.setattr("lies.cli.xdg.config_home", lambda: tmp_path)
    monkeypatch.setattr("lies.cli.xdg.cache_home", lambda: tmp_path)
    monkeypatch.setattr("lies.cli.xdg.state_home", lambda: tmp_path)
    monkeypatch.setattr("lies.wiki.wiki.xdg.runtime_dir_for", lambda n: tmp_path / "run" / n)

    from lies.mcp.server import ingest_source

    result = ingest_source(source=http_url, collection="alpha", name=name)
    assert isinstance(result, str)
    # Same XDG-derived path correction as the CLI test: see comment above.
    cfg_root = tmp_path / "lies" / name / "collections"
    assert (cfg_root / "alpha.yaml").exists()
