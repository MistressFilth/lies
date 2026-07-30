"""WikiMemoryService.

The single owner of wiki mutation, git commit, and qmd refresh for
invisible memory. Plans are validated, applied, and committed
atomically. The qmd derived index is refreshed after the git commit
and any refresh failure is reported as a non-fatal ``qmd_stale``
condition in the receipt.
"""
from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from lies.memory.index import append_log_entry, rebuild_index
from lies.memory.models import (
    EvidenceAppend,
    MemoryPlan,
    MemoryReceipt,
    OperationKind,
    PageCreate,
    PageReference,
    PageUpdate,
    WikiCommitFailed,
    WikiPlanInvalid,
    WikiWriteConflict,
)
from lies.memory.validation import (
    validate_operation_evidence,
    validate_page_path,
    validate_page_type,
)
from lies.qmd.cli import qmd_update
from lies.wiki.git import CommitError, atomic_commit
from lies.wiki.layout import WikiLayout

_QMD_STALE_PREFIX = "qmd_stale"
_LOCK = threading.Lock()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_page(layout: WikiLayout, path: str) -> str:
    try:
        resolved = validate_page_path(layout, path)
    except WikiPlanInvalid:
        return ""
    if not resolved.exists():
        return ""
    try:
        return resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _page_type_from_dir(directory_name: str) -> str:
    """Convert a plural wiki subdirectory name to its singular page type.

    ``validate_page_type`` accepts singular forms (``concept``, ``entity``)
    while wiki pages live under plural subdirectories (``concepts``,
    ``entities``). The on-disk convention is plural; the type vocabulary is
    singular. This helper bridges them so the service can call
    ``validate_page_type`` without bypassing it.
    """
    if directory_name.endswith("ies"):
        return directory_name[:-3] + "y"
    return directory_name.removesuffix("s")


class WikiMemoryService:
    """Apply validated memory plans to the wiki at ``layout``."""

    def __init__(
        self,
        layout: WikiLayout,
        *,
        qmd_update: Callable[[Path], None] = qmd_update,
    ) -> None:
        self._layout = layout
        self._qmd_update = qmd_update

    def hash_page(self, path: str) -> str:
        """Return the sha256 of the current page content, or empty string."""
        content = _read_page(self._layout, path)
        if not content:
            return ""
        return _hash_text(content)

    def current_state(self, path: str) -> tuple[str, str]:
        """Return ``(sha256, content)`` for the current page state."""
        content = _read_page(self._layout, path)
        return (_hash_text(content) if content else "", content)

    def validate_plan(self, plan: MemoryPlan) -> None:
        """Validate a plan without applying it. Raises typed errors."""
        for op in plan.operations:
            validate_operation_evidence(op)
            try:
                resolved = validate_page_path(self._layout, op.path)
            except WikiPlanInvalid as exc:
                raise WikiPlanInvalid(str(exc), path=op.path) from exc
            page_type = _page_type_from_dir(resolved.parent.name)
            validate_page_type(page_type)
            if isinstance(op, (PageUpdate, EvidenceAppend)):
                current = _read_page(self._layout, op.path)
                expected = op.expected_sha256
                actual = _hash_text(current) if current else ""
                if actual != expected:
                    raise WikiWriteConflict(
                        f"hash mismatch for {op.path}: "
                        f"expected {expected[:12]}..., got {actual[:12]}..."
                    )

    def apply_plan(self, plan: MemoryPlan) -> MemoryReceipt:
        """Apply ``plan`` atomically and return a receipt."""
        with _LOCK:
            self.validate_plan(plan)
            if plan.is_noop():
                return MemoryReceipt(
                    changed_pages=[],
                    deferred=[],
                    fallback_used=False,
                    fallback_reason="",
                    errors=[],
                )
            try:
                changed = self._apply_operations(plan)
            except WikiPlanInvalid:
                self._restore_index()
                raise
            self._commit_or_rollback(plan)
            qmd_ok, qmd_msg = self._refresh_qmd()
            return MemoryReceipt(
                changed_pages=changed,
                deferred=[],
                fallback_used=False,
                fallback_reason="",
                errors=[] if qmd_ok else [qmd_msg],
            )

    def _apply_operations(self, plan: MemoryPlan) -> list[PageReference]:
        changed: list[PageReference] = []
        for op in plan.operations:
            resolved = validate_page_path(self._layout, op.path)
            resolved.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(op, PageCreate):
                resolved.write_text(op.content, encoding="utf-8")
                kind = OperationKind.CREATE
            elif isinstance(op, PageUpdate):
                existing = _read_page(self._layout, op.path)
                if _hash_text(existing) != op.expected_sha256:
                    raise WikiWriteConflict(f"hash mismatch for {op.path}")
                resolved.write_text(op.content, encoding="utf-8")
                kind = OperationKind.UPDATE
            elif isinstance(op, EvidenceAppend):
                existing = _read_page(self._layout, op.path)
                if _hash_text(existing) != op.expected_sha256:
                    raise WikiWriteConflict(f"hash mismatch for {op.path}")
                resolved.write_text(
                    existing.rstrip() + "\n\n" + op.content, encoding="utf-8"
                )
                kind = OperationKind.APPEND
            else:
                raise WikiPlanInvalid(f"unsupported operation: {op!r}")
            changed.append(
                PageReference(
                    path=op.path,
                    collection_id=self._layout.root.name,
                    op=kind,
                )
            )
        rebuild_index(self._layout)
        for op in plan.operations:
            append_log_entry(
                self._layout,
                f"## [{datetime.now(tz=timezone.utc).date().isoformat()}] "
                f"memory | {op.kind.value} | {op.path}",
            )
        return changed

    def _restore_index(self) -> None:
        try:
            rebuild_index(self._layout)
        except OSError:
            pass

    def _commit_or_rollback(self, plan: MemoryPlan) -> None:
        try:
            atomic_commit(self._layout.root, f"memory: {plan.rationale}")
        except CommitError as exc:
            self._restore_index()
            raise WikiCommitFailed(f"commit failed: {exc}") from exc

    def _refresh_qmd(self) -> tuple[bool, str]:
        try:
            self._qmd_update(self._layout.root)
        except Exception as exc:  # noqa: BLE001 - report, do not roll back git
            # We deliberately do not roll back the git commit; the
            # wiki is correct. The caller decides whether to retry.
            return (False, f"{_QMD_STALE_PREFIX}: {exc}")
        return (True, "")
