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

from lies.agents.page_writer import PageDiff, PageOperation
from lies.lock_errors import (  # noqa: F401 — Task 5/6/7 will reference these from this module.
    WikiFlockCorrupt,
    WikiFlockIndeterminate,
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
    PageDelete,
    PageReference,
    PageUpdate,
    WikiCollectionRef,
    WikiLockBusy,
    WikiPlanInvalid,
    WikiSearchResult,
    WikiWriteConflict,
    _PlanOperation,
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
from lies.utils.exclusive import MAX_FLOCK_AGE_S, acquire_create_lock, release_create_lock
from lies.utils.lock_heartbeat import Heartbeat, write_heartbeat, write_owner_pid
from lies.wiki.git import atomic_commit
from lies.wiki.wiki import Wiki

_QMD_STALE_PREFIX = "qmd_stale"


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@contextlib.contextmanager
def _acquire_wiki_flock(wiki: Wiki, *, force_repair: bool = False) -> Iterator[None]:
    """Acquire a non-blocking exclusive flock on the wiki's memory flock.

    Yields once on success. Raises :class:`WikiLockBusy` on a live
    contender (whose pid + start time surface in the error message so
    operators can identify the holder), :class:`WikiFlockIndeterminate`
    when the contender's liveness cannot be determined (EPERM on
    ``os.kill``) and the heartbeat is older than the recovery window,
    or :class:`WikiFlockUnrepairable` if a ``force_repair=True`` retry
    still loses (in which case the pid file is already gone — the
    message directs the operator to run ``lies flock <wiki> status``
    to inspect the current contender).

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
    if result is None:
        if force_repair:
            raise WikiFlockUnrepairable(
                f"memory flock for wiki '{wiki.name}' could not be "
                f"force-reaped; a live contender won the second attempt. "
                f"Run `lies flock {wiki.name} status` to inspect, then "
                f"`lies flock {wiki.name} force-repair` or kill the "
                f"contender manually."
            )
        raise WikiLockBusy(f"wiki memory lock is held by another process: {create_lock}")
    if result.status == "indeterminate":
        t = result.holder_started_at
        started_at = datetime.fromtimestamp(t, tz=UTC).isoformat() if t is not None else "unknown"
        raise WikiFlockIndeterminate(
            f"{wiki.name} flock held by an indeterminate process "
            f"(pid {result.holder_pid}, started {started_at}); "
            f"cannot determine live state. "
            f"Run `lies flock {wiki.name} force-repair` to inspect/retry "
            f"or kill {result.holder_pid} manually."
        )
    if result.status == "busy":
        p = result.holder_pid
        t = result.holder_started_at
        if p is not None and t is not None:
            t_iso = datetime.fromtimestamp(t, tz=UTC).isoformat()
            raise WikiLockBusy(
                f"memory flock for wiki '{wiki.name}' held by live pid {p} "
                f"(started {t_iso}); run `lies flock {wiki.name} status` "
                f"to inspect or kill {p} manually."
            )
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
    except (OSError, UnicodeDecodeError):
        return ""


def translate_page_diffs_to_plan(
    diffs: list[PageDiff],
    *,
    collection: str,
    source_path: str,
    sha_lookup: Callable[[str], str] | None = None,
) -> MemoryPlan:
    """Map page-writer output to a MemoryPlan with tag="ingest".

    Each ``PageDiff`` becomes one operation carrying the source path
    as its sole evidence reference. ``PageUpdate`` requires
    ``sha_lookup``; ``PageCreate`` and ``PageDelete`` do not.
    """
    operations: list[_PlanOperation] = []
    for diff in diffs:
        rel = diff.path.as_posix() if isinstance(diff.path, Path) else str(diff.path)
        if diff.operation == PageOperation.CREATE:
            if diff.new_content is None:
                raise WikiPlanInvalid(f"CREATE op missing new_content: {rel}")
            operations.append(
                PageCreate(
                    path=rel,
                    content=diff.new_content,
                    evidence=[source_path],
                    tag="ingest",
                )
            )
        elif diff.operation == PageOperation.UPDATE:
            if diff.new_content is None:
                raise WikiPlanInvalid(f"UPDATE op missing new_content: {rel}")
            if sha_lookup is None:
                raise WikiPlanInvalid(
                    f"UPDATE op {rel} requires sha_lookup so expected_sha256 can be set"
                )
            operations.append(
                PageUpdate(
                    path=rel,
                    expected_sha256=sha_lookup(rel),
                    content=diff.new_content,
                    evidence=[source_path],
                    tag="ingest",
                )
            )
        elif diff.operation == PageOperation.DELETE:
            operations.append(PageDelete(path=rel, evidence=[source_path], tag="ingest"))
        else:
            raise WikiPlanInvalid(f"unsupported PageOperation: {diff.operation!r}")
    rationale = f"ingest {source_path} into {collection}"
    return MemoryPlan(
        operations=operations,
        rationale=rationale,
        evidence=[source_path],
    )


def build_synthesis_plan(
    *,
    question: str,
    answer: str,
    pages_read: list[str],
    collection: str,
    sha_lookup: Callable[[str], str] | None = None,
    exists: Callable[[str], bool] | None = None,
) -> MemoryPlan:
    """Build a single-op MemoryPlan that files a synthesis page.

    Slug: ``<slugify(question)[:48]-<sha256(question)[:8]>.md``.
    Path: ``wiki/<collection>/synthesis/<slug>``.
    Body: agent's answer + ``## Evidence`` section listing each
    page in ``pages_read`` as ``[[slug]]``.
    Frontmatter: ``title``, ``collection``, ``tags: [synthesis]``,
    ``sources``, ``derived_from: pages_read``.
    Returns ``PageCreate`` if the slug does not exist; ``PageUpdate``
    otherwise (the latter requires ``sha_lookup``).

    Raises:
        WikiPlanInvalid: ``pages_read`` is empty (no evidence), or
            collision detected without ``sha_lookup`` provided.
    """
    if not pages_read:
        raise WikiPlanInvalid("pages_read is empty; nothing to file")

    safe_slug = _slugify(question)[:48].strip("-") or "synthesis"
    digest = hashlib.sha256(question.encode("utf-8")).hexdigest()[:8]
    rel_path = f"{collection}/synthesis/{safe_slug}-{digest}.md"

    body = _format_synthesis_body(
        question=question,
        answer=answer,
        pages_read=pages_read,
    )

    collision = exists is not None and exists(rel_path)
    if collision:
        if sha_lookup is None:
            raise WikiPlanInvalid(f"collision on {rel_path} but sha_lookup not provided")
        op: _PlanOperation = PageUpdate(
            path=rel_path,
            expected_sha256=sha_lookup(rel_path),
            content=body,
            evidence=list(pages_read),
            tag="synthesis",
        )
    else:
        op = PageCreate(
            path=rel_path,
            content=body,
            evidence=list(pages_read),
            tag="synthesis",
        )

    return MemoryPlan(
        operations=[op],
        rationale=f"synthesis for question: {question[:120]}",
        evidence=list(pages_read),
    )


def _slugify(text: str) -> str:
    """Lowercase, replace non-alphanumeric with '-', collapse runs."""
    out = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        else:
            out.append("-")
    slug = "".join(out).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug


def _format_synthesis_body(*, question: str, answer: str, pages_read: list[str]) -> str:
    """Build the full markdown body: frontmatter + answer + Evidence."""
    title = question.strip().rstrip("?.!").strip() or "Synthesis"
    frontmatter_lines = [
        "---",
        f"title: {title}",
        "type: synthesis",
        f"collection: {pages_read[0].split('/')[0] if pages_read else 'unknown'}",
        "tags: [synthesis]",
        "sources:",
    ]
    for p in pages_read:
        frontmatter_lines.append(f"  - {p}")
    frontmatter_lines.append("derived_from:")
    for p in pages_read:
        frontmatter_lines.append(f"  - {p}")
    frontmatter_lines.append("---")
    frontmatter_lines.append("")

    evidence_lines = ["## Evidence", ""]
    for p in pages_read:
        evidence_lines.append(f"- [[{p}]]")
    evidence_lines.append("")

    return "\n".join(frontmatter_lines) + answer.strip() + "\n\n" + "\n".join(evidence_lines)


def _page_type_from_dir(directory_name: str) -> str:
    """Convert a plural wiki subdirectory name to its singular page type.

    ``validate_page_type`` accepts singular forms (``concept``, ``entity``)
    while wiki pages live under plural subdirectories (``concepts``,
    ``entities``). The on-disk convention is plural; the type vocabulary is
    singular. This helper bridges them so the service can call
    ``validate_page_type`` without bypassing it.

    A small allow-list handles words whose plural form is identical to the
    singular (``synthesis``): naively stripping the trailing ``s`` would
    mangle them.
    """
    if directory_name == "synthesis":
        return "synthesis"
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
            # Normalize the path the same way ``_apply_operations``
            # does: defensively strip ``wiki/`` twice so a
            # doubled-prefix input (``op.path = "wiki/wiki/log.md"``)
            # is recognized as a system file rather than a page under
            # a ``wiki/`` subdirectory. Without this normalization the
            # validate-side system-file checks (the
            # ``op.path == "wiki/log.md"`` and ``resolved == log_path``
            # tests below) miss the doubled-prefix shape and the op
            # either slips through to ``validate_page_type("wiki")`` or
            # falls through to the apply-side guard with the wrong
            # resolved path. The apply path strips twice too; keeping
            # the two in lockstep ensures a typo-bypass attempt is
            # caught at validate time with the same error the apply
            # path would raise.
            normalized = op.path.removeprefix("wiki/").removeprefix("wiki/")
            try:
                resolved = validate_page_path(self._wiki, normalized)
            except WikiPlanInvalid as exc:
                raise WikiPlanInvalid(str(exc), path=op.path) from exc
            is_index = op.path == "wiki/index.md" or resolved == self._wiki.wiki_dir / "index.md"
            is_log = op.path == "wiki/log.md" or resolved == self._wiki.wiki_dir / "log.md"
            if is_index:
                resolved = self._wiki.wiki_dir / "index.md"
            elif is_log:
                resolved = self._wiki.wiki_dir / "log.md"
            page_type = _page_type_from_dir(resolved.parent.name)
            if not is_index and not is_log:
                validate_page_type(page_type)
            if not isinstance(op, (PageCreate, PageUpdate, EvidenceAppend, PageDelete)):
                raise WikiPlanInvalid(f"unsupported operation: {op!r}", path=op.path)
            if not is_index and not is_log and isinstance(op, (PageCreate, PageUpdate)):
                try:
                    validate_frontmatter(parse_frontmatter(op.content), page_type=page_type)
                except WikiPlanInvalid as exc:
                    raise WikiPlanInvalid(str(exc), path=op.path) from exc
            if isinstance(op, PageCreate) and resolved.exists() and not (is_index or is_log):
                raise WikiPlanInvalid(
                    "page already exists; use UPDATE or APPEND",
                    path=op.path,
                )
            if isinstance(op, (PageUpdate, EvidenceAppend)):
                index_path = self._wiki.wiki_dir / "index.md"
                log_path = self._wiki.wiki_dir / "log.md"
                if is_index:
                    current = index_path.read_text(encoding="utf-8")
                elif is_log:
                    current = log_path.read_text(encoding="utf-8") if log_path.exists() else None
                else:
                    # Use the normalized path so the read targets the
                    # same on-disk file the apply path will write to.
                    # Page-writer emits paths with the ``wiki/`` prefix
                    # per the schema convention; ``_read_page`` joins
                    # onto ``wiki.wiki_dir`` so a bare ``op.path``
                    # would resolve to ``<data_root>/wiki/wiki/<rest>``
                    # and silently miss the file. The hash comparison
                    # then sees ``""`` (= missing) regardless of the
                    # real on-disk content, so ``validate_plan`` and
                    # ``apply_plan`` must agree on the resolved path.
                    current = _read_page(self._wiki, normalized)
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
                files = self._collect_commit_files(plan, changed)
                if not files:
                    self._restore_working_tree(repo, snapshot_ref)
                    return self._empty_receipt()
                pages_list = ", ".join(op.path for op in plan.operations)
                ops_hist = {
                    kind.value: sum(1 for op in plan.operations if op.kind == kind)
                    for kind in {op.kind for op in plan.operations}
                }
                ops_str = " ".join(f"{k}={v}" for k, v in sorted(ops_hist.items()))
                evidence_count = len(getattr(plan, "evidence", []) or [])
                tag = next(iter(plan.operations)).tag
                commit_message = (
                    f"{tag}: {plan.rationale}\n\n"
                    f"Pages: {pages_list}\n"
                    f"Ops: {ops_str}\n"
                    f"Evidence: {evidence_count}\n"
                )
                commit_sha = atomic_commit(
                    self._wiki.data_root,
                    commit_message,
                    files=files,
                )
                if commit_sha is None:
                    # atomic_commit detected the staged diff was empty
                    # (e.g. an idempotent plan wrote byte-identical content).
                    # No commit landed. Return an empty receipt rather
                    # than claiming changed_pages that were not really
                    # applied at the git level.
                    self._restore_working_tree(repo, snapshot_ref)
                    return self._empty_receipt()
            except BaseException:
                self._restore_working_tree(repo, snapshot_ref)
                raise
            self._discard_snapshot(repo, snapshot_ref)
            sidecar_errors: list[str] = []
            try:
                from lies.memory.sidecar import append_receipt

                append_receipt(self._wiki, plan, commit_sha, evidence_count=evidence_count)
            except Exception as exc:  # noqa: BLE001 - non-fatal: receipt surface
                sidecar_errors.append(f"sidecar_append_failed: {exc}")
            qmd_ok, qmd_msg = self._refresh_qmd()
            errors = list(sidecar_errors)
            if not qmd_ok:
                errors.append(qmd_msg)
            return MemoryReceipt(
                changed_pages=changed,
                deferred=[],
                fallback_used=False,
                fallback_reason="",
                errors=errors,
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
        log_path = self._wiki.wiki_dir / "log.md"
        for op in plan.operations:
            # Page-writer emits paths with the ``wiki/`` prefix per the
            # schema convention; ``validate_page_path`` joins onto
            # ``wiki.wiki_dir`` (= ``<data_root>/wiki``), so the bare
            # ``op.path`` would otherwise resolve to
            # ``<data_root>/wiki/wiki/<rest>`` — outside the per-collection
            # subdir convention. Strip the leading ``wiki/`` before
            # resolving so the file lands at the correct on-disk path.
            # Strip the leading ``wiki/`` defensively twice so a
            # doubled-prefix input (``op.path = "wiki/wiki/log.md"``)
            # can't bypass the system-file guard below. After the
            # double-strip the resolved-on-disk path matches
            # ``log_path`` / ``index_path`` and the guard fires; a
            # single-strip would leave the resolved path at
            # ``<data_root>/wiki/wiki/<file>`` and the guard's
            # bare-name + resolved-equality checks would both miss.
            rel = op.path.removeprefix("wiki/").removeprefix("wiki/")
            resolved = validate_page_path(self._wiki, rel)
            if resolved == index_path:
                resolved = index_path
            resolved.parent.mkdir(parents=True, exist_ok=True)
            # System-file guard: ``wiki/index.md`` and ``wiki/log.md`` are
            # rebuilt/extended by the service itself (``rebuild_index``
            # and ``append_log_entry`` below) — never written or removed
            # by an op. Block ALL op kinds against these paths so the
            # agent can never bypass the rebuild/append envelope by
            # routing a write through a different op shape.
            #
            # Carve-out: ``PageUpdate`` on ``index_path`` is the
            # established pathway for the repair agent's ``UpdateIndex``
            # operation (see :func:`from_repair_plan`). The page is
            # transiently rewritten with the catalog update, then
            # overwritten by ``rebuild_index`` below — the ``PageUpdate``
            # is the receipt surface for the operation, not a permanent
            # write. ``log_path`` has no analogous pathway because
            # ``append_log_entry`` already owns log mutation.
            #
            # Both the bare-name form (``op.path == "log.md"``) and the
            # qualified form (``op.path == "wiki/log.md"``) reach the
            # dispatcher's guard: after the double ``wiki/`` strip above,
            # ``"wiki/log.md"`` resolves to ``wiki.wiki_dir/log.md``
            # (= ``log_path``) and is matched by the resolved equality,
            # while a bare ``log.md`` also resolves to ``log_path``. A
            # pathological ``"wiki/wiki/log.md"`` input also resolves to
            # ``log_path`` after the double strip, so the guard fires
            # (a single strip would have left it at
            # ``<data_root>/wiki/wiki/log.md`` and bypassed the guard).
            # In every case the guard raises and the op-shape-specific
            # branches below never see those paths.
            is_system_log = resolved == log_path
            is_system_index = resolved == index_path
            if is_system_log:
                raise WikiPlanInvalid(f"cannot write to {op.path}: system file")
            if is_system_index and not isinstance(op, PageUpdate):
                raise WikiPlanInvalid(f"cannot write to {op.path}: system file")
            if isinstance(op, PageCreate):
                resolved.write_text(op.content, encoding="utf-8")
                kind = OperationKind.CREATE
            elif isinstance(op, PageUpdate):
                # ``log_path`` is blocked by the guard above; only the
                # ``index_path`` carve-out reaches this branch. All other
                # targets fall through to ``_read_page`` — pass the
                # stripped ``rel`` so the read targets the same path as
                # the write above.
                if resolved == index_path:
                    existing = index_path.read_text(encoding="utf-8")
                else:
                    existing = _read_page(self._wiki, rel)
                actual = "" if existing is None else _hash_text(existing)
                if actual != op.expected_sha256:
                    raise WikiWriteConflict(f"hash mismatch for {op.path}")
                resolved.write_text(op.content, encoding="utf-8")
                kind = OperationKind.UPDATE
            elif isinstance(op, EvidenceAppend):
                # Both ``log_path`` and ``index_path`` are blocked by the
                # guard above. Every surviving op targets a non-system
                # page, so the unconditional ``_read_page`` is correct.
                # Pass the stripped ``rel`` so the read targets the same
                # path as the write above.
                existing = _read_page(self._wiki, rel)
                actual = "" if existing is None else _hash_text(existing)
                if actual != op.expected_sha256:
                    raise WikiWriteConflict(f"hash mismatch for {op.path}")
                base = "" if existing is None else existing
                resolved.write_text(base.rstrip() + "\n\n" + op.content, encoding="utf-8")
                kind = OperationKind.APPEND
            elif isinstance(op, PageDelete):
                if not resolved.exists():
                    # No-op: file already absent. Skip the PageReference
                    # so the receipt reflects that no change occurred at
                    # this op's target. ``rebuild_index`` and
                    # ``append_log_entry`` (below) still run for the rest
                    # of the plan.
                    continue
                resolved.unlink()
                kind = OperationKind.DELETE
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
                f"{op.tag} | {op.kind.value} | {op.path}",
            )
        return changed

    def _collect_commit_files(self, plan: MemoryPlan, changed: list[PageReference]) -> list[str]:
        """Compute the repo-relative paths to commit for ``plan``.

        Driven by ``changed`` (the ``PageReference`` list returned by
        ``_apply_operations``) rather than the raw ``plan.operations``
        so a successful ``PageDelete`` is staged even though ``unlink``
        already removed it from disk. ``git add`` records the removal
        when the path is passed; if the path is omitted the working
        tree keeps the file uncommitted and the next ``apply_plan``'s
        snapshot/restore resurrects it.

        A no-op ``PageDelete`` (file never existed) leaves no
        ``PageReference`` in ``changed`` and therefore no entry in
        ``candidates`` — ``git add`` is never asked to stage a
        never-existed path (it returns ``fatal: pathspec ... did not
        match any files``).

        System files (``wiki/index.md``, ``wiki/log.md``) are added
        unconditionally; the trailing ``.exists()`` filter drops
        ``wiki/log.md`` only on a fresh repo where ``append_log_entry``
        has not yet created it. Returns a sorted, de-duplicated list of
        repo-relative POSIX paths.
        """
        root = self._wiki.data_root
        index_path = self._wiki.wiki_dir / "index.md"
        candidates: set[str] = set()
        for ref in changed:
            try:
                # Strip the leading ``wiki/`` before resolving so
                # ``validate_page_path`` produces the same on-disk path
                # the write landed at in ``_apply_operations``. The
                # ``git add`` pathspec must match the actual file
                # location; otherwise the commit fails with
                # ``pathspec ... did not match any files``.
                ref_rel = ref.path.removeprefix("wiki/")
                resolved = validate_page_path(self._wiki, ref_rel)
            except WikiPlanInvalid:
                continue  # validate_plan should have rejected this already
            # ``validate_page_path`` joins the (already-stripped) path
            # onto ``wiki.wiki_dir``. A literal ``"wiki/index.md"`` input
            # (e.g. the repair agent's ``UpdateIndex`` →
            # ``PageUpdate(path="wiki/index.md")``) would otherwise
            # resolve to ``wiki.wiki_dir/wiki/index.md`` instead of the
            # real catalog at ``wiki.wiki_dir/index.md``. Remap to the
            # on-disk path before computing the repo-relative form.
            if ref.path == "wiki/index.md" or resolved == index_path:
                resolved = index_path
            try:
                rel = resolved.relative_to(root).as_posix()
            except ValueError:
                continue
            candidates.add(rel)
        candidates.add("wiki/index.md")
        candidates.add("wiki/log.md")
        return sorted(
            p
            for p in candidates
            if p not in ("wiki/index.md", "wiki/log.md") or (root / p).exists()
        )

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
