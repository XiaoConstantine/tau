import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock

import pytest

from tau_coding.paths import TauPaths
from tau_coding.session_manager import CodingSessionRecord, SessionManager


def test_session_manager_creates_and_lists_sessions(tmp_path: Path) -> None:
    manager = SessionManager(TauPaths(home=tmp_path / ".tau", agents_home=tmp_path / ".agents"))
    cwd = tmp_path / "project"
    cwd.mkdir()

    record = manager.create_session(
        cwd=cwd,
        model="fake",
        provider_name="fake-provider",
        title="Test session",
    )

    assert record.provider_name == "fake-provider"
    assert record.path.parent.parent == tmp_path / ".tau" / "sessions"
    assert "project-" in record.path.parent.name
    assert len(record.path.parent.name.rsplit("-", maxsplit=1)[-1]) == 6
    assert (record.path.parent / "index.jsonl").exists()
    assert not (tmp_path / ".tau" / "sessions" / "index.jsonl").exists()
    assert record.path.name == f"{record.id}.jsonl"
    assert manager.get_session(record.id) == record
    assert manager.list_sessions() == [record]
    assert manager.list_sessions(cwd) == [record]


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\u0085"])
def test_session_manager_round_trips_unicode_line_separator_in_title(
    tmp_path: Path, separator: str
) -> None:
    manager = SessionManager(TauPaths(home=tmp_path / ".tau", agents_home=tmp_path / ".agents"))
    cwd = tmp_path / "project"
    cwd.mkdir()

    record = manager.create_session(cwd=cwd, model="fake", title=f"line one{separator}line two")

    assert manager.get_session(record.id) == record
    assert manager.list_sessions(cwd) == [record]


def test_session_manager_ignores_and_repairs_invalid_index_lines(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    manager = SessionManager(TauPaths(home=tmp_path / ".tau", agents_home=tmp_path / ".agents"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    first = manager.create_session(cwd=cwd, model="fake", session_id="first")
    index_path = manager.project_index_path(cwd)
    with index_path.open("a", encoding="utf-8") as index_file:
        index_file.write("}\n")

    with caplog.at_level("WARNING"):
        assert manager.list_sessions(cwd) == [first]

    assert f"{index_path}:2" in caplog.text
    second = manager.prepare_session(cwd=cwd, model="fake", session_id="second")
    manager.index_session(second)
    lines = index_path.read_text(encoding="utf-8").splitlines()
    assert {json.loads(line)["id"] for line in lines} == {"first", "second"}


def test_session_manager_atomic_write_preserves_existing_index_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = SessionManager(TauPaths(home=tmp_path / ".tau", agents_home=tmp_path / ".agents"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    manager.create_session(cwd=cwd, model="fake", session_id="existing")
    index_path = manager.project_index_path(cwd)
    original_content = index_path.read_text(encoding="utf-8")
    replacement = manager.prepare_session(cwd=cwd, model="fake", session_id="replacement")

    def fail_fsync(_file_descriptor: int) -> None:
        raise OSError("simulated fsync failure")

    monkeypatch.setattr("tau_coding.session_manager.os.fsync", fail_fsync)

    with pytest.raises(OSError, match="simulated fsync failure"):
        manager._write_index(index_path, [replacement])

    assert index_path.read_text(encoding="utf-8") == original_content
    assert list(index_path.parent.glob(f".{index_path.name}.*.tmp")) == []


def test_session_manager_serializes_concurrent_index_updates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = TauPaths(home=tmp_path / ".tau", agents_home=tmp_path / ".agents")
    first_manager = SessionManager(paths)
    second_manager = SessionManager(paths)
    cwd = tmp_path / "project"
    cwd.mkdir()
    first = first_manager.prepare_session(cwd=cwd, model="fake", session_id="first")
    second = second_manager.prepare_session(cwd=cwd, model="fake", session_id="second")
    original_write = SessionManager._write_index
    counter_lock = Lock()
    release_first_write = Event()
    first_write_started = Event()
    second_write_started = Event()
    second_update_started = Event()
    write_count = 0

    def blocking_write(
        manager: SessionManager, path: Path, records: list[CodingSessionRecord]
    ) -> None:
        nonlocal write_count
        with counter_lock:
            write_count += 1
            current_write = write_count
        if current_write == 1:
            first_write_started.set()
            if not release_first_write.wait(timeout=5):
                raise TimeoutError("test did not release first index writer")
        else:
            second_write_started.set()
        original_write(manager, path, records)

    def index_second_session() -> None:
        second_update_started.set()
        second_manager.index_session(second)

    monkeypatch.setattr(SessionManager, "_write_index", blocking_write)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(first_manager.index_session, first)
        assert first_write_started.wait(timeout=5)
        second_future = executor.submit(index_second_session)
        assert second_update_started.wait(timeout=5)
        try:
            assert not second_write_started.wait(timeout=0.2)
        finally:
            release_first_write.set()
        first_future.result(timeout=5)
        second_future.result(timeout=5)

    assert second_write_started.is_set()
    assert {record.id for record in first_manager.list_sessions(cwd)} == {"first", "second"}


def test_session_manager_prepares_unindexed_session(tmp_path: Path) -> None:
    manager = SessionManager(TauPaths(home=tmp_path / ".tau", agents_home=tmp_path / ".agents"))
    cwd = tmp_path / "project"
    cwd.mkdir()

    record = manager.prepare_session(cwd=cwd, model="fake", provider_name="fake-provider")

    assert record.provider_name == "fake-provider"
    assert record.path.name == f"{record.id}.jsonl"
    assert manager.get_session(record.id) is None
    assert manager.list_sessions(cwd) == []

    indexed = manager.index_session(record)

    assert indexed == record
    assert manager.get_session(record.id) == record
    assert manager.list_sessions(cwd) == [record]


def test_session_manager_filters_sessions_by_project_cwd(tmp_path: Path) -> None:
    manager = SessionManager(TauPaths(home=tmp_path / ".tau", agents_home=tmp_path / ".agents"))
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()

    first = manager.create_session(cwd=first_cwd, model="fake", title="First")
    second = manager.create_session(cwd=second_cwd, model="fake", title="Second")

    assert manager.list_sessions(first_cwd) == [first]
    assert manager.list_sessions(second_cwd) == [second]
    assert {record.id for record in manager.list_sessions()} == {first.id, second.id}


def test_session_manager_returns_latest_session_for_cwd(tmp_path: Path) -> None:
    manager = SessionManager(TauPaths(home=tmp_path / ".tau", agents_home=tmp_path / ".agents"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    older = manager.create_session(cwd=cwd, model="older", session_id="older")
    newer = manager.create_session(cwd=cwd, model="newer", session_id="newer")
    manager.touch_session(older.id)

    latest = manager.latest_session_for_cwd(cwd)

    assert latest is not None
    assert latest.id == older.id
    assert latest.model == "older"
    assert newer in manager.list_sessions(cwd)


def test_session_manager_ignores_extra_index_metadata(tmp_path: Path) -> None:
    manager = SessionManager(TauPaths(home=tmp_path / ".tau", agents_home=tmp_path / ".agents"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    index_path = manager.project_index_path(cwd)
    session_path = index_path.parent / "session-1.jsonl"
    index_path.parent.mkdir(parents=True)
    index_path.write_text(
        json.dumps(
            {
                "id": "session-1",
                "path": str(session_path),
                "cwd": str(cwd.resolve()),
                "model": "gpt-5",
                "title": "Session",
                "created_at": 1.0,
                "updated_at": 2.0,
                "provider_name": "openai-codex",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    [record] = manager.list_sessions(cwd)

    assert record.id == "session-1"
    assert record.path == session_path
    assert record.model == "gpt-5"


def test_session_manager_gets_or_creates_default_session(tmp_path: Path) -> None:
    manager = SessionManager(TauPaths(home=tmp_path / ".tau", agents_home=tmp_path / ".agents"))
    cwd = tmp_path / "project"
    cwd.mkdir()

    first = manager.get_or_create_default_session(
        cwd=cwd, model="fake", provider_name="fake-provider"
    )
    second = manager.get_or_create_default_session(cwd=cwd, model="other")

    assert first == second
    assert first.provider_name == "fake-provider"
    assert first.id.startswith("default-")
    assert first.path.name == "default.jsonl"
    assert first.path.parent.exists()


def test_session_manager_touch_updates_metadata(tmp_path: Path) -> None:
    manager = SessionManager(TauPaths(home=tmp_path / ".tau", agents_home=tmp_path / ".agents"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    record = manager.create_session(cwd=cwd, model="fake")

    updated = manager.touch_session(
        record.id,
        model="new-model",
        provider_name="new-provider",
        title="Updated",
    )

    assert updated is not None
    assert updated.id == record.id
    assert updated.model == "new-model"
    assert updated.provider_name == "new-provider"
    assert updated.title == "Updated"
    assert updated.updated_at >= record.updated_at
    assert manager.get_session(record.id) == updated


def test_session_manager_sorts_newest_updated_first(tmp_path: Path) -> None:
    manager = SessionManager(TauPaths(home=tmp_path / ".tau", agents_home=tmp_path / ".agents"))
    cwd = tmp_path / "project"
    cwd.mkdir()
    older = manager.create_session(cwd=cwd, model="fake", session_id="older")
    newer = manager.create_session(cwd=cwd, model="fake", session_id="newer")
    manager.touch_session(older.id)

    sessions = manager.list_sessions()

    assert [session.id for session in sessions] == ["older", "newer"]
    assert newer in sessions
