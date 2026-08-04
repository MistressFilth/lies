"""qmd integration: CLI shell-out and MCP client.

The qmd tool (https://github.com/tobi/qmd) is LIES's indexer and search backend.
This package exposes two surfaces:

* `lies.qmd.cli` — subprocess wrapper for batch/maintenance commands
  (`qmd update`, `qmd status`, `qmd collection add/remove`, `qmd ls`,
  `qmd query`). Use these from non-agent code paths.
* `lies.qmd.mcp` — connection config for the qmd MCP server, used by
  pydantic-ai agents for native tool calling.
"""

from lies.qmd.capability import QmdCapability
from lies.qmd.cli import (
    QmdCommandError,
    QmdError,
    QmdNoResultsError,
    QmdNotInstalledError,
    is_qmd_installed,
    qmd_collection_add,
    qmd_ls,
    qmd_query,
    qmd_status,
    qmd_update,
)
from lies.qmd.mcp import QmdMcpClient

__all__ = [
    "QmdCapability",
    "QmdCommandError",
    "QmdError",
    "QmdMcpClient",
    "QmdNoResultsError",
    "QmdNotInstalledError",
    "is_qmd_installed",
    "qmd_collection_add",
    "qmd_ls",
    "qmd_query",
    "qmd_status",
    "qmd_update",
]
