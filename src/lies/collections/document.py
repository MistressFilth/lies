"""Document record — one normalized doc within a collection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class DocumentStatus(str, Enum):
    OK = "ok"
    QUARANTINED = "quarantined"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class Document:
    path: str
    source_sha256: str
    ingested_sha256: str
    ingested_at: datetime
    collection: str
    status: DocumentStatus

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            try:
                coerced = DocumentStatus(self.status)
            except ValueError as exc:
                raise ValueError(f"invalid status: {self.status!r}") from exc
            object.__setattr__(self, "status", coerced)
        elif not isinstance(self.status, DocumentStatus):
            raise ValueError(f"invalid status: {self.status!r}")  # noqa: TRY004
