"""Wiki dataclass: role-routed path accessors."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lies import xdg
from lies.constants import LIES_DATA_SUBDIR
from lies.errors import WikiNotRegistered
from lies.wiki.validation import validate_name


@dataclass(frozen=True)
class Wiki:
    """A wiki identified by ``name``, with role-routed XDG paths."""

    name: str
    data_root: Path
    config_root: Path
    cache_root: Path
    state_root: Path
    runtime_root: Path

    @classmethod
    def data_root_for(cls, name: str) -> Path:
        validate_name(name)
        return xdg.data_home() / LIES_DATA_SUBDIR / name

    @classmethod
    def require(cls, name: str) -> Wiki:
        validate_name(name)
        data_root = cls.data_root_for(name)
        if not data_root.exists():
            raise WikiNotRegistered(name, xdg.data_home())
        return cls(
            name=name,
            data_root=data_root,
            config_root=xdg.config_home() / LIES_DATA_SUBDIR / name,
            cache_root=xdg.cache_home() / LIES_DATA_SUBDIR / name,
            state_root=xdg.state_home() / LIES_DATA_SUBDIR / name,
            runtime_root=xdg.runtime_dir_for(name),
        )

    @property
    def raw_dir(self) -> Path:
        return self.data_root / "raw"

    @property
    def wiki_dir(self) -> Path:
        return self.data_root / "wiki"

    @property
    def schema_path(self) -> Path:
        return self.config_root / "schema.md"

    @property
    def collections_dir(self) -> Path:
        return self.config_root / "collections"

    @property
    def providers_path(self) -> Path:
        """Path to user-level ``providers.toml``.

        This file is intentionally user-level (not per-wiki). All wikis share
        one providers catalog; per-wiki model overrides are not supported.
        """
        return xdg.config_home() / LIES_DATA_SUBDIR / "providers.toml"

    @property
    def settings_path(self) -> Path:
        return self.config_root / "lies.toml"

    @property
    def hashes_dir(self) -> Path:
        return self.cache_root / "hashes"

    @property
    def logs_dir(self) -> Path:
        return self.state_root / "logs"

    @property
    def scratch_dir(self) -> Path:
        return self.state_root / "scratch"

    @property
    def poison_root(self) -> Path:
        return self.state_root / "poison"

    @property
    def memory_lock_path(self) -> Path:
        return self.runtime_root / "memory.lock"

    @property
    def sync_lock_path(self) -> Path:
        return self.runtime_root / "sync.lock"

    @property
    def sync_create_lock_path(self) -> Path:
        return self.runtime_root / "sync.lock.create"

    @property
    def sync_fd_path(self) -> Path:
        return self.runtime_root / "sync.lock.fd"

    @property
    def registry_path(self) -> Path:
        """Path to the per-wiki collection registry JSON."""
        return self.state_root / "registry.json"

    @property
    def mcp_pid_path(self) -> Path:
        return self.runtime_root / "mcp.pid"

    @property
    def mcp_create_lock_path(self) -> Path:
        return self.runtime_root / "mcp.pid.create"

    @property
    def mcp_log_path(self) -> Path:
        return self.state_root / "mcp.log"

    @property
    def memory_create_lock_path(self) -> Path:
        """Atomic-create sentinel for the memory flock.

        ``WikiMemoryService._acquire_wiki_flock`` opens this with
        ``O_CREAT | O_EXCL``; whoever wins claims the flock.
        """
        return self.runtime_root / "memory.lock.create"

    @property
    def memory_pid_path(self) -> Path:
        """Holder-PID file for memory flock stale-recovery."""
        return self.runtime_root / "memory.pid"

    @property
    def memory_heartbeat_path(self) -> Path:
        """Heartbeat JSON for memory flock stale-recovery."""
        return self.runtime_root / "memory.state.json"
