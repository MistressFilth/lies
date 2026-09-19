"""Wiki dataclass: role-routed path accessors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING

from lies import xdg
from lies.constants import LIES_DATA_SUBDIR
from lies.errors import WikiNotRegistered
from lies.wiki.validation import validate_name

if TYPE_CHECKING:
    from lies.schema.sections import SectionContract


# Per-name migration fallback table. Maps wiki name -> a no-arg callable
# returning a tuple of alternative data_root paths to probe in order if
# the primary (``xdg.data_home() / LIES_DATA_SUBDIR / <name>``) does
# not exist. Callables (not static tuples) so each call re-resolves
# ``xdg.data_home()`` — tests monkeypatch ``xdg.data_home`` and expect
# the fallback to track the patched value. First existing wins. The
# returned Wiki keeps the requested ``name``; only ``data_root`` falls
# back — other xdg-role paths (config_root, cache_root, state_root,
# runtime_root) still resolve via ``name``.
#
# 2026-08-15: default wiki's data_root was renamed from
# <xdg>/lies/default/ to <xdg>/lies/wiki/ during the bare-repo
# migration. Only the default wiki has this history.
_MIGRATION_FALLBACKS: dict[str, Callable[[], tuple[Path, ...]]] = {
    "default": lambda: (xdg.data_home() / LIES_DATA_SUBDIR / "wiki",),
}


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
        primary = cls.data_root_for(name)
        builder = _MIGRATION_FALLBACKS.get(name)
        fallbacks: tuple[Path, ...] = builder() if builder else ()
        candidates = (primary, *fallbacks)
        data_root: Path | None = None
        for candidate in candidates:
            if candidate.exists():
                data_root = candidate
                break
        if data_root is None:
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

    @cached_property
    def section_contract(self) -> SectionContract:
        """Resolved per-type required-section contract.

        Resolution order:
          1. per-wiki ``schema.md`` (operator override)
          2. shipped ``default_schema.md``
          3. empty contract (no enforcement)

        Cached for the lifetime of the :class:`Wiki` instance; one
        parser call per wiki open.

        Override semantics (per F17 design): full-replacement, not
        merge. An override file with a ``## Section contract`` block
        replaces the default wholesale — types omitted from the
        block lose their required sections. An override file
        without the block falls back to the default.

        Limitation: detection of "block present" uses the heuristic
        ``any(for_type(t) for t in fields)``. An override declaring
        every type as ``(none)`` will fall back to default. This is
        not exercised by the override tests; if it matters, add
        a sibling ``has_section_contract_block(markdown)`` helper
        to ``lies.schema.loader`` and switch this check to it.
        """
        # Local import to keep the monkeypatch target
        # (``lies.schema.loader.parse_section_contract``) effective
        # against the caching test, and to defer the import past
        # wiki-class definition (loader.py imports :class:`Wiki`).
        from lies.schema.loader import parse_section_contract
        from lies.schema.sections import SectionContract

        # Per-wiki override takes precedence.
        override_path = self.config_root / "schema.md"
        if override_path.exists():
            text = override_path.read_text(encoding="utf-8")
            parsed = parse_section_contract(text)
            if any(parsed.for_type(t) for t in SectionContract.model_fields.keys()):
                # Override block present (at least one type carries
                # required headings). Full-replacement semantics.
                return parsed
            # Override file exists but has no Section contract block;
            # fall back to default.
        # Default schema lives in the package.
        from importlib.resources import files

        default_text = (
            files("lies.schema").joinpath("default_schema.md").read_text(encoding="utf-8")
        )
        return parse_section_contract(default_text)

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
