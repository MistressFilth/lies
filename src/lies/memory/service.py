"""WikiMemoryService.

The single owner of wiki mutation, git commit, and qmd refresh for
invisible memory. Plans are validated, applied, and committed
atomically. The qmd derived index is refreshed after the git commit
and any refresh failure is reported as a non-fatal ``qmd_stale``
condition in the receipt.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lies.agents.repair_models import RepairPlan

from lies.lock_errors import (  # noqa: F401 — Task 5/6/7 will reference these from this module.
    WikiFlockCorrupt,
    WikiFlockStale,
    WikiFlockUnrepairable,
)
from lies.memory.index import append_log_entry, rebuild_index
from lies.memory.models import (
    EvidenceAppend,
    MemoryPlan,
    MemoryReceipt,
    OperationKind,
    PageCreate,
    PageReference,
    PageUpdate,
    WikiCollectionRef,
    WikiLockBusy,
    WikiPlanInvalid,
    WikiSearchResult,
    WikiWriteConflict,
)
from lies.memory.retrieval import _path_for_id, read_pages, search_wiki
from lies.memory.validation import (
    parse_frontmatter,
    validate_frontmatter,
    validate_operation_evidence,
    validate_page_path,
    validate_page_type,
)
from lies.qmd.cli import qmd_update
from lies.utils.exclusive import acquire_create_lock, release_create_lock
from lies.utils.lock_heartbeat import Heartbeat, write_heartbeat, write_owner_pid
from lies.wiki.git import atomic_commit
from lies.wiki.wiki import Wiki

_QMD_STALE_PREFIX = "qmd_stale"
MAX_FLOCK_AGE_S = 2 * 3600  # 2h ceiling on memory flock liveness


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@contextlib.contextmanager
def _acquire_wiki_flock(wiki: Wiki, *, force_repair: bool = False) -> Iterator[None]:
    """Acquire a non-blocking exclusive flock on the wiki's memory flock.

    Yields once on success. On contention, the underlying
    :func:`lies.utils.exclusive.acquire_create_lock` reap loop
    auto-recovers from a dead holder; a still-alive competitor raises
    :class:`WikiLockBusy`. With ``force_repair=True``, the underlying
    acquire escalates to an unconditional reap + retry; if the retry
    still loses (a live contender recreates the create-lock between
    reap and retry) the caller sees :class:`WikiFlockUnrepairable`
    with an operator-actionable message. Releases by unlinking the
    lock files + pid + state JSON on exit.

    The lock envelope lives under ``$XDG_RUNTIME_DIR/lies/<wiki>/``
    (not the wiki's data root), so no gitignore coordination is
    needed. Each wiki has its own directory containing:
    ``memory.lock`` (the lock itself), ``memory.lock.create`` (atomic
    sentinel), ``memory.pid`` (owner-PID text), ``memory.state.json``
    (heartbeat).
    """
    create_lock = wiki.memory_create_lock_path
    pid_path = wiki.memory_pid_path
    state_path = wiki.memory_heartbeat_path

    create_lock.parent.mkdir(parents=True, exist_ok=True)
    result = acquire_create_lock(
        create_lock,
        max_age_s=MAX_FLOCK_AGE_S,
        pid_path=pid_path,
        state_json_path=state_path,
        force_repair=force_repair,
    )
    if result is None or result.status == "busy":
        if force_repair:
            # Under force_repair the underlying acquire already
            # attempted an unconditional reap + retry. Still busy
            # means a live contender recreated the envelope between
            # reap and retry; only manual ``lies flock <name>
            # force-repair`` (or process teardown) can break it.
            raise WikiFlockUnrepairable(
                f"memory flock for wiki '{wiki.name}' held by a live "
                f"process even after force-repair; manual intervention "
                f"required. Run `lies flock {wiki.name} force-repair`."
            )
        # After the one reap-retry, still busy. Manual force-repair is
        # the only path forward.
        raise WikiLockBusy(f"wiki memory lock is held by another process: {create_lock}")

    write_owner_pid(pid_path, os.getpid())
    write_heartbeat(
        state_path,
        Heartbeat(pid=os.getpid(), started_at=time.time(), scope=wiki.name),
    )
    try:
        yield
    finally:
        release_create_lock(
            create_lock,
            result.fd,
            pid_path=pid_path,
            state_json_path=state_path,
        )


def _read_page(wiki: Wiki, path: str) -> str | None:
    """Read page content; return ``None`` if the file does not exist.

    Distinguishes "missing file" from "empty file": a missing file
    returns ``None`` (callers map it to the empty-string ``""`` sentinel);
    an empty file returns ``""``. This keeps ``hash_page`` consistent
    with ``apply_plan``'s hash-mismatch detection, where empty
    content is a valid baseline rather than a missing-page sentinel.
    """
    try:
        resolved = validate_page_path(wiki, path)
    except WikiPlanInvalid:
        return None
    if not resolved.exists():
        return None
    try:
        return resolved.read_text(encoding="utf-8")
    except OSError, UnicodeDecodeError:
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
    """Apply validated memory plans to ``wiki``."""

    def __init__(
        self,
        wiki: Wiki,
        *,
        qmd_update: Callable[[Path], None] = qmd_update,
    ) -> None:
        self._wiki = wiki
        self._qmd_update = qmd_update
        self._lock = threading.Lock()
        self._known_evidence: set[str] = set()
        # Local import: ``lies.collections.registry`` re-exports through
        # ``lies.collections.__init__`` which imports this module's
        # ``WikiMemoryService`` via ``lies.memory.__init__`` — keeping the
        # import at module scope creates a cycle when test files load the
        # registry module in isolation.
        from lies.collections.registry import Registry

        on_disk = Registry.load(wiki)
        live = Registry.filter_stale(on_disk, wiki)
        self._registered: dict[str, WikiCollectionRef] = dict(live.collections)

    @contextlib.contextmanager
    def _acquire_flock(self, *, force_repair: bool = False) -> Iterator[None]:
        """Acquire the cross-process flock for this wiki.

        ``force_repair=True`` escalates contention: the underlying
        :func:`_acquire_wiki_flock` will unconditionally reap a held
        envelope and retry once before surfacing
        :class:`WikiFlockUnrepairable`. Without the flag, a live
        contender raises :class:`WikiLockBusy` as before.
        """
        with _acquire_wiki_flock(self._wiki, force_repair=force_repair):
            yield

    @property
    def known_evidence(self) -> frozenset[str]:
        """Evidence references authenticated by this service during the turn."""
        return frozenset(self._known_evidence)

    def register_evidence(self, references: set[str]) -> None:
        """Register citation references supplied by another bounded read surface."""
        self._known_evidence.update(reference for reference in references if reference)

    def register_collection(self, ref: WikiCollectionRef) -> None:
        """Register ``ref`` in memory and on disk.

        Idempotent in-memory and on-disk: read-merge-write under
        atomic temp+rename. Failure to persist raises
        :class:`RegistryWriteFailed`; the in-memory dict is still
        updated before the write so the current process sees the
        registration immediately.
        """
        # Local import: see ``__init__`` for the cycle rationale.
        from lies.collections.registry import Registry

        self._registered[ref.collection_id] = ref
        on_disk = Registry.load(self._wiki)
        merged = Registry.merge(on_disk, Registry(collections=dict(self._registered)))
        live = Registry.filter_stale(merged, self._wiki)
        Registry.save(self._wiki, live)

    def is_registered(self, collection_id: str) -> bool:
        return collection_id in self._registered

    def registered_collections(self) -> list[WikiCollectionRef]:
        return list(self._registered.values())

    def search(
        self,
        question: str,
        *,
        collection_ids: list[str] | None = None,
        limit: int = 5,
    ) -> WikiSearchResult:
        """Search this wiki and authenticate the returned evidence references."""

        collection_id = self._wiki.name
        if collection_ids is not None and collection_id not in collection_ids:
            return WikiSearchResult(
                query=question,
                pages=[],
                truncated=False,
                fallback_used=False,
                fallback_reason="collection_filtered",
            )
        result = search_wiki(self._wiki, question, limit=limit)
        for page in result.pages:
            self._known_evidence.update(
                {
                    page.page_id,
                    page.path,
                    f"{page.path}:{page.line_start}-{page.line_end}",
                    page.excerpt,
                }
            )
        return result

    def read(self, page_ids: list[str]) -> dict[str, str]:
        """Read authenticated page IDs through the service retrieval boundary."""
        from lies.memory.models import WikiPageNotFound

        unknown = [page_id for page_id in page_ids if _path_for_id(self._wiki, page_id) is None]
        if unknown:
            raise WikiPageNotFound(f"unknown page_ids: {unknown}")
        bodies = read_pages(self._wiki, page_ids)
        self._known_evidence.update(bodies)
        return bodies

    def hash_page(self, path: str) -> str:
        """Return the sha256 of the current page content, or empty sentinel.

        A missing file returns the empty string ``""`` (sentinel);
        an empty file returns the SHA-256 of the empty string.
        """
        content = _read_page(self._wiki, path)
        if content is None:
            return ""
        return _hash_text(content)

    def current_state(self, path: str) -> tuple[str, str]:
        """Return ``(sha256, content)`` for the current page state.

        ``sha256`` is the SHA-256 of the empty string for an empty file
        and ``""`` for a missing file. ``content`` is always the
        on-disk content (``""`` for empty or missing).
        """
        content = _read_page(self._wiki, path)
        if content is None:
            return ("", "")
        return (_hash_text(content), content)

    def validate_plan(self, plan: MemoryPlan) -> None:
        """Validate a plan without applying it. Raises typed errors."""
        for op in plan.operations:
            validate_operation_evidence(op, known_references=self._known_evidence)
            try:
                resolved = validate_page_path(self._wiki, op.path)
            except WikiPlanInvalid as exc:
                raise WikiPlanInvalid(str(exc), path=op.path) from exc
            is_index = op.path == "wiki/index.md" or resolved == self._wiki.wiki_dir / "index.md"
            if is_index:
                resolved = self._wiki.wiki_dir / "index.md"
            page_type = _page_type_from_dir(resolved.parent.name)
            if not is_index:
                validate_page_type(page_type)
            if not isinstance(op, (PageCreate, PageUpdate, EvidenceAppend)):
                raise WikiPlanInvalid(f"unsupported operation: {op!r}", path=op.path)
            if not is_index and isinstance(op, (PageCreate, PageUpdate)):
                try:
                    validate_frontmatter(parse_frontmatter(op.content), page_type=page_type)
                except WikiPlanInvalid as exc:
                    raise WikiPlanInvalid(str(exc), path=op.path) from exc
            if isinstance(op, PageCreate) and resolved.exists():
                raise WikiPlanInvalid(
                    "page already exists; use UPDATE or APPEND",
                    path=op.path,
                )
            if isinstance(op, (PageUpdate, EvidenceAppend)):
                index_path = self._wiki.wiki_dir / "index.md"
                current = (
                    index_path.read_text(encoding="utf-8")
                    if is_index
                    else _read_page(self._wiki, op.path)
                )
                actual = "" if current is None else _hash_text(current)
                if actual != op.expected_sha256:
                    raise WikiWriteConflict(
                        f"hash mismatch for {op.path}: "
                        f"expected {op.expected_sha256[:12]}..., "
                        f"got {actual[:12]}..."
                    )

    def apply_plan(self, plan: MemoryPlan, *, force_repair: bool = False) -> MemoryReceipt:
        """Apply ``plan`` atomically and return a receipt.

        The wiki is snapshotted before any on-disk write so a failure
        during ``_apply_operations`` or the git commit can roll the
        working tree back to its pre-apply state. The qmd refresh runs
        after the commit and reports ``qmd_stale`` non-fatally.

        ``force_repair=True`` escalates flock contention: the
        cross-process envelope is unconditionally reaped + retried
        once before surfacing :class:`WikiFlockUnrepairable`. Without
        the flag, a live contender raises :class:`WikiLockBusy` as
        before.
        """
        with self._acquire_flock(force_repair=force_repair), self._lock:
            self.validate_plan(plan)
            if plan.is_noop():
                return self._empty_receipt()
            repo = self._wiki.data_root
            snapshot_ref = self._snapshot_working_tree(repo)
            try:
                changed = self._apply_operations(plan)
            except BaseException:
                self._restore_working_tree(repo, snapshot_ref)
                raise
            try:
                files = self._collect_commit_files(plan)
                if not files:
                    self._restore_working_tree(repo, snapshot_ref)
                    return self._empty_receipt()
                atomic_commit(
                    self._wiki.data_root,
                    f"memory: {plan.rationale}",
                    files=files,
                )
            except BaseException:
                self._restore_working_tree(repo, snapshot_ref)
                raise
            self._discard_snapshot(repo, snapshot_ref)
            qmd_ok, qmd_msg = self._refresh_qmd()
            return MemoryReceipt(
                changed_pages=changed,
                deferred=[],
                fallback_used=False,
                fallback_reason="",
                errors=[] if qmd_ok else [qmd_msg],
            )

    def apply_repair_plan(self, plan: RepairPlan, *, force_repair: bool = False) -> MemoryReceipt:
        """Apply a RepairPlan under the same envelope as apply_plan.

        Repair evidence is authenticated before translation so the normal
        memory validation path accepts the bounded repair findings.

        ``force_repair`` flows through to :meth:`apply_plan` and onward
        into the cross-process flock acquisition; see there for the
        contention semantics.
        """
        from lies.memory.repair import from_repair_plan

        self.register_evidence(set(plan.evidence))
        for op in plan.operations:
            self.register_evidence(set(getattr(op, "evidence", [])))
        memory_plan = from_repair_plan(plan, wiki=self._wiki)
        return self.apply_plan(memory_plan, force_repair=force_repair)

    def _apply_operations(self, plan: MemoryPlan) -> list[PageReference]:
        changed: list[PageReference] = []
        index_path = self._wiki.wiki_dir / "index.md"
        for op in plan.operations:
            resolved = validate_page_path(self._wiki, op.path)
            if op.path == "wiki/index.md" or resolved == index_path:
                resolved = index_path
            resolved.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(op, PageCreate):
                resolved.write_text(op.content, encoding="utf-8")
                kind = OperationKind.CREATE
            elif isinstance(op, PageUpdate):
                existing = (
                    index_path.read_text(encoding="utf-8")
                    if resolved == index_path
                    else _read_page(self._wiki, op.path)
                )
                actual = "" if existing is None else _hash_text(existing)
                if actual != op.expected_sha256:
                    raise WikiWriteConflict(f"hash mismatch for {op.path}")
                resolved.write_text(op.content, encoding="utf-8")
                kind = OperationKind.UPDATE
            elif isinstance(op, EvidenceAppend):
                existing = _read_page(self._wiki, op.path)
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
                    collection_id=self._wiki.name,
                    op=kind,
                )
            )
        rebuild_index(self._wiki)
        for op in plan.operations:
            append_log_entry(
                self._wiki,
                f"## [{datetime.now(tz=UTC).date().isoformat()}] "
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
        root = self._wiki.data_root
        index_path = self._wiki.wiki_dir / "index.md"
        candidates: set[str] = set()
        for op in plan.operations:
            try:
                resolved = validate_page_path(self._wiki, op.path)
            except WikiPlanInvalid:
                continue  # validate_plan should have rejected this already
            if op.path == "wiki/index.md" or resolved == index_path:
                resolved = index_path
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
            rebuild_index(self._wiki)
        except OSError:
            pass

    def _refresh_qmd(self) -> tuple[bool, str]:
        try:
            self._qmd_update(self._wiki.data_root)
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
