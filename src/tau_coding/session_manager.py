"""User-home session management for Tau coding sessions."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import time
from typing import BinaryIO
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from tau_coding.paths import TauPaths

logger = logging.getLogger(__name__)


class SessionRecordModel(BaseModel):
    """JSON-serializable coding-session metadata."""

    model_config = ConfigDict(extra="ignore")

    id: str
    path: str
    cwd: str
    model: str
    provider_name: str | None = None
    title: str | None = None
    created_at: float
    updated_at: float


@dataclass(frozen=True, slots=True)
class CodingSessionRecord:
    """Metadata for one durable coding session."""

    id: str
    path: Path
    cwd: Path
    model: str
    title: str | None
    created_at: float
    updated_at: float
    provider_name: str | None = None

    @classmethod
    def from_model(cls, model: SessionRecordModel) -> CodingSessionRecord:
        """Convert a JSON model to a record."""
        return cls(
            id=model.id,
            path=Path(model.path),
            cwd=Path(model.cwd),
            model=model.model,
            title=model.title,
            created_at=model.created_at,
            updated_at=model.updated_at,
            provider_name=model.provider_name,
        )

    def to_model(self) -> SessionRecordModel:
        """Convert this record to a JSON model."""
        return SessionRecordModel(
            id=self.id,
            path=str(self.path),
            cwd=str(self.cwd),
            model=self.model,
            title=self.title,
            created_at=self.created_at,
            updated_at=self.updated_at,
            provider_name=self.provider_name,
        )


class SessionManager:
    """Create, index, list, and resume user-home coding sessions."""

    def __init__(self, paths: TauPaths | None = None) -> None:
        self.paths = paths or TauPaths()

    @property
    def index_path(self) -> Path:
        """Return the legacy global session metadata index path."""
        return self.paths.sessions_dir / "index.jsonl"

    def project_index_path(self, cwd: Path) -> Path:
        """Return the session metadata index path for a project cwd."""
        return self.paths.project_session_dir(cwd) / "index.jsonl"

    def list_sessions(self, cwd: Path | None = None) -> list[CodingSessionRecord]:
        """Return indexed sessions, newest updated first.

        When `cwd` is provided, only sessions for that resolved working directory
        are returned. Without `cwd`, records are aggregated across project
        indexes and the legacy global index.
        """
        records = self._read_project_records(cwd) if cwd is not None else self._read_all_records()
        return sorted(records, key=lambda record: record.updated_at, reverse=True)

    def get_session(self, session_id: str) -> CodingSessionRecord | None:
        """Return a session record by id, if present."""
        for record in self._read_all_records():
            if record.id == session_id:
                return record
        return None

    def latest_session_for_cwd(self, cwd: Path) -> CodingSessionRecord | None:
        """Return the most recently updated session for a working directory."""
        records = self.list_sessions(cwd)
        return records[0] if records else None

    def create_session(
        self,
        *,
        cwd: Path,
        model: str,
        provider_name: str | None = None,
        title: str | None = None,
        session_id: str | None = None,
    ) -> CodingSessionRecord:
        """Create and index a new session record."""
        record = self.prepare_session(
            cwd=cwd,
            model=model,
            provider_name=provider_name,
            title=title,
            session_id=session_id,
        )
        self.index_session(record)
        return record

    def prepare_session(
        self,
        *,
        cwd: Path,
        model: str,
        provider_name: str | None = None,
        title: str | None = None,
        session_id: str | None = None,
    ) -> CodingSessionRecord:
        """Return metadata for a session without adding it to the resume index."""
        now = time()
        resolved_cwd = cwd.resolve()
        record_id = session_id or uuid4().hex
        path = self.paths.project_session_dir(resolved_cwd) / f"{record_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        return CodingSessionRecord(
            id=record_id,
            path=path,
            cwd=resolved_cwd,
            model=model,
            provider_name=provider_name,
            title=title,
            created_at=now,
            updated_at=now,
        )

    def index_session(self, record: CodingSessionRecord) -> CodingSessionRecord:
        """Add a prepared session record to the resume index."""
        self._upsert(record)
        return record

    def get_or_create_default_session(
        self, *, cwd: Path, model: str, provider_name: str | None = None
    ) -> CodingSessionRecord:
        """Return the default project session, creating an index record when needed."""
        resolved_cwd = cwd.resolve()
        project_hash = self.paths.project_session_dir(resolved_cwd).name
        session_id = f"default-{project_hash}"
        existing = self.get_session(session_id)
        if existing is not None:
            return existing

        now = time()
        path = self.paths.default_session_path(resolved_cwd)
        record = CodingSessionRecord(
            id=session_id,
            path=path,
            cwd=resolved_cwd,
            model=model,
            provider_name=provider_name,
            title="Default session",
            created_at=now,
            updated_at=now,
        )
        self._upsert(record)
        return record

    def touch_session(
        self,
        session_id: str,
        *,
        model: str | None = None,
        provider_name: str | None = None,
        title: str | None = None,
    ) -> CodingSessionRecord | None:
        """Update a session's last-used metadata."""
        existing = self.get_session(session_id)
        if existing is None:
            return None
        updated = CodingSessionRecord(
            id=existing.id,
            path=existing.path,
            cwd=existing.cwd,
            model=model or existing.model,
            provider_name=provider_name if provider_name is not None else existing.provider_name,
            title=title if title is not None else existing.title,
            created_at=existing.created_at,
            updated_at=time(),
        )
        self._upsert(updated)
        return updated

    def _read_index(self, path: Path) -> list[CodingSessionRecord]:
        if not path.exists():
            return []

        records: list[CodingSessionRecord] = []
        # Split on newlines only: str.splitlines() would also split on characters
        # like U+2028 that appear unescaped inside JSON string values.
        for line_number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                model = SessionRecordModel.model_validate_json(stripped)
            except ValidationError as exc:
                logger.warning(
                    "Ignoring invalid session index entry at %s:%d: %s",
                    path,
                    line_number,
                    exc,
                )
                continue
            records.append(CodingSessionRecord.from_model(model))
        return records

    def _read_project_records(self, cwd: Path) -> list[CodingSessionRecord]:
        resolved_cwd = cwd.resolve()
        records = self._read_index(self.project_index_path(resolved_cwd))
        records.extend(
            record for record in self._read_index(self.index_path) if record.cwd == resolved_cwd
        )
        return _deduplicate_records(records)

    def _read_all_records(self) -> list[CodingSessionRecord]:
        records = self._read_index(self.index_path)
        for index_path in self.paths.sessions_dir.glob("*/index.jsonl"):
            records.extend(self._read_index(index_path))
        return _deduplicate_records(records)

    def _write_index(self, path: Path, records: list[CodingSessionRecord]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        content = "\n".join(record.to_model().model_dump_json() for record in records)
        if content:
            content += "\n"
        _atomic_write_text(path, content)

    def _upsert(self, record: CodingSessionRecord) -> None:
        path = self.project_index_path(record.cwd)
        lock_path = path.with_name(f".{path.name}.lock")
        with _exclusive_file_lock(lock_path):
            records = [item for item in self._read_index(path) if item.id != record.id]
            records.append(record)
            self._write_index(path, records)


def _atomic_write_text(path: Path, content: str) -> None:
    """Atomically replace *path* with durable UTF-8 text."""
    temp_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path.replace(path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """Serialize index updates across Tau processes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock_file:
        _acquire_file_lock(lock_file)
        try:
            yield
        finally:
            _release_file_lock(lock_file)


def _acquire_file_lock(lock_file: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        _ensure_lock_byte(lock_file)
        lock_file.seek(0)
        locking = msvcrt.locking  # type: ignore[attr-defined]
        lock_mode = msvcrt.LK_LOCK  # type: ignore[attr-defined]
        locking(lock_file.fileno(), lock_mode, 1)
        return

    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)


def _release_file_lock(lock_file: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        locking = msvcrt.locking  # type: ignore[attr-defined]
        unlock_mode = msvcrt.LK_UNLCK  # type: ignore[attr-defined]
        locking(lock_file.fileno(), unlock_mode, 1)
        return

    import fcntl

    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _ensure_lock_byte(lock_file: BinaryIO) -> None:
    """Give Windows an existing byte range for ``msvcrt.locking``."""
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"\0")
        lock_file.flush()
    lock_file.seek(0)


def _deduplicate_records(records: list[CodingSessionRecord]) -> list[CodingSessionRecord]:
    by_id: dict[str, CodingSessionRecord] = {}
    for record in records:
        existing = by_id.get(record.id)
        if existing is None or record.updated_at >= existing.updated_at:
            by_id[record.id] = record
    return list(by_id.values())
