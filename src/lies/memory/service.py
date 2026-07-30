"""WikiMemoryService.

The single owner of wiki mutation, git commit, and qmd refresh for
invisible memory. Plans are validated, applied, and committed
atomically. The qmd derived index is refreshed after the git commit
and any refresh failure is reported as a non-fatal ``qmd_stale``
condition in the receipt.
"""

from __future__ import annotations

import hashlib
import subprocess
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
    WikiPlanInvalid,
    WikiWriteConflict,
)
from lies.memory.validation import (
    validate_operation_evidence,
    validate_page_path,
    validate_page_type,
)
from lies.qmd.cli import qmd_update
from lies.wiki.git import atomic_commit
from lies.wiki.layout import WikiLayout

_QMD_STALE_PREFIX = "qmd_stale"
_LOCK = threading.Lock()


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_page(layout: WikiLayout, path: str) -> str | None:
    """Read page content; return ``None`` if the file does not exist.

    Distinguishes "missing file" from "empty file": a missing file
    returns ``None`` (callers map it to the empty-string ``""`` sentinel);
    an empty file returns ``""``. This keeps ``hash_page`` consistent
    with ``validate_plan``'s hash-mismatch detection, where empty
    content is a valid baseline rather than a missing-page sentinel.
    """
    try:
        resolved = validate_page_path(layout, path)
    except WikiPlanInvalid:
        return None
    if not resolved.exists():
        return None
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


def _run_git(
    args: list[str], repo: Path, *, check: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run a git subprocess; default to non-raising ``check=False``."""
    return subprocess.run(
        args,
        cwd=repo,
        capture_output=True,
        text=True,
        check=check,
    )


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
        """Return the sha256 of the current page content, or empty sentinel.

        A missing file returns the empty string ``""`` (sentinel);
        an empty file returns the SHA-256 of the empty string.
        """
        content = _read_page(self._layout, path)
        if content is None:
            return ""
        return _hash_text(content)

    def current_state(self, path: str) -> tuple[str, str]:
        """Return ``(sha256, content)`` for the current page state.

        ``sha256`` is the SHA-256 of the empty string for an empty file
        and ``""`` for a missing file. ``content`` is always the
        on-disk content (``""`` for empty or missing).
        """
        content = _read_page(self._layout, path)
        if content is None:
            return ("", "")
        return (_hash_text(content), content)

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
                actual = "" if current is None else _hash_text(current)
                if actual != op.expected_sha256:
                    raise WikiWriteConflict(
                        f"hash mismatch for {op.path}: "
                        f"expected {op.expected_sha256[:12]}..., "
                        f"got {actual[:12]}..."
                    )

    def apply_plan(self, plan: MemoryPlan) -> MemoryReceipt:
        """Apply ``plan`` atomically and return a receipt.

        The wiki is snapshotted before any on-disk write so a failure
        during ``_apply_operations`` or the git commit can roll the
        working tree back to its pre-apply state. The qmd refresh runs
        after the commit and reports ``qmd_stale`` non-fatally.
        """
        with _LOCK:
            self.validate_plan(plan)
            if plan.is_noop():
                return self._empty_receipt()
            repo = self._layout.root
            snapshot_ref = self._snapshot_working_tree(repo)
            try:
                changed = self._apply_operations(plan)
            except BaseException:
                self._restore_working_tree(repo, snapshot_ref)
                raise
            # ``_apply_operations`` succeeded; keep its writes and drop
            # the snapshot. If the commit itself fails, roll the tree
            # back to the pre-apply state.
            self._discard_snapshot(repo, snapshot_ref)
            try:
                files = self._collect_commit_files(plan)
                if not files:
                    # Nothing to commit (no candidate files exist on
                    # disk). Treat as a no-op and roll back any partial
                    # writes so the wiki is untouched.
                    self._restore_working_tree(repo, snapshot_ref)
                    return self._empty_receipt()
                atomic_commit(
                    self._layout.root,
                    f"memory: {plan.rationale}",
                    files=files,
                )
            except BaseException:
                self._restore_working_tree(repo, snapshot_ref)
                raise
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
                actual = "" if existing is None else _hash_text(existing)
                if actual != op.expected_sha256:
                    raise WikiWriteConflict(f"hash mismatch for {op.path}")
                resolved.write_text(op.content, encoding="utf-8")
                kind = OperationKind.UPDATE
            elif isinstance(op, EvidenceAppend):
                existing = _read_page(self._layout, op.path)
                actual = "" if existing is None else _hash_text(existing)
                if actual != op.expected_sha256:
                    raise WikiWriteConflict(f"hash mismatch for {op.path}")
                base = "" if existing is None else existing
                resolved.write_text(base.rstrip() + "\n\n" + op.content, encoding="utf-8")
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

    def _collect_commit_files(self, plan: MemoryPlan) -> list[str]:
        """Compute the repo-relative paths to commit for ``plan``.

        Includes every op's target page plus ``wiki/index.md`` and
        ``wiki/log.md``. Files that do not exist on disk are skipped so
        a fresh repo (where ``wiki/log.md`` may not yet exist) does not
        pass an empty list to ``atomic_commit``. Returns a sorted,
        de-duplicated list of repo-relative POSIX paths.
        """
        root = self._layout.root
        candidates: set[str] = set()
        for op in plan.operations:
            try:
                resolved = validate_page_path(self._layout, op.path)
            except WikiPlanInvalid:
                continue  # validate_plan should have rejected this already
            try:
                rel = resolved.relative_to(root).as_posix()
            except ValueError:
                continue
            candidates.add(rel)
        candidates.add("wiki/index.md")
        candidates.add("wiki/log.md")
        return sorted(p for p in candidates if (root / p).exists())

    def _restore_index(self) -> None:
        try:
            rebuild_index(self._layout)
        except OSError:
            pass

    def _refresh_qmd(self) -> tuple[bool, str]:
        try:
            self._qmd_update(self._layout.root)
        except Exception as exc:  # noqa: BLE001 - report, do not roll back git
            # We deliberately do not roll back the git commit; the
            # wiki is correct. The caller decides whether to retry.
            return (False, f"{_QMD_STALE_PREFIX}: {exc}")
        return (True, "")

    @staticmethod
    def _empty_receipt() -> MemoryReceipt:
        return MemoryReceipt(
            changed_pages=[],
            deferred=[],
            fallback_used=False,
            fallback_reason="",
            errors=[],
        )

    # -- host-side snapshot / rollback ----------------------------------------
    #
    # Mirrors ``Orchestrator.run_ingest``: stash the working tree (including
    # untracked files) before mutating the wiki, drop the stash on success,
    # restore the tree from the stash on any failure. ``atomic_commit``
    # already rolls back the staging area on ``CommitError``; the stash
    # pattern here covers the working-tree writes that ``atomic_commit``
    # leaves untouched.

    @staticmethod
    def _snapshot_working_tree(repo: Path) -> str:
        """Stash any working-tree changes; return a stash ref.

        If the working tree is clean, returns the sentinel ``"<clean>"``
        so the restore path knows there's nothing to put back.
        """
        result = _run_git(
            ["git", "stash", "push", "--include-untracked", "-m", "pre-apply"],
            repo,
        )
        if result.returncode != 0:
            raise RuntimeError(f"failed to snapshot working tree: {result.stderr.strip()}")
        if "No local changes to save" in result.stdout:
            return "<clean>"
        return "stash@{0}"

    @staticmethod
    def _restore_working_tree(repo: Path, snapshot_ref: str) -> None:
        """Restore the working tree from a snapshot, wiping agent changes.

        Used on the failure path of ``apply_plan`` to put the wiki back
        to the pre-apply state. Consumes the snapshot (drops the stash
        entry) as part of restoration.
        """
        # ``git checkout -- .`` covers tracked modifications; ``git
        # clean -fd`` covers untracked files and directories (new
        # pages the apply created).
        _run_git(["git", "checkout", "--", "."], repo)
        _run_git(["git", "clean", "-fd"], repo)
        if snapshot_ref == "<clean>":
            return
        pop = _run_git(["git", "stash", "pop"], repo)
        if pop.returncode != 0:
            # The stash pop conflicted (e.g. the apply touched files
            # the user had dirty). Drop the stash and surface a clear
            # error so the pre-existing dirty state is preserved in
            # the stash list for the user to recover.
            _run_git(["git", "stash", "drop"], repo)
            raise RuntimeError(
                "could not restore pre-apply working tree: stash pop "
                "conflicted. Original state is preserved in the stash list."
            )

    @staticmethod
    def _discard_snapshot(repo: Path, snapshot_ref: str) -> None:
        """Drop the stash entry without applying it."""
        if snapshot_ref == "<clean>":
            return
        _run_git(["git", "stash", "drop", snapshot_ref], repo)
