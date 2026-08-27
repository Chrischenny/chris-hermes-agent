from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from chris_hermes_agent.migrations import SCHEMA_VERSION, initialize_database
from chris_hermes_agent.store import ConcurrentUpdateError, TaskRepository
from chris_hermes_agent.task_models import (
    CheckpointRecord,
    ContextSegmentRecord,
    EventType,
    SessionContextState,
    TaskRecord,
    TaskStatus,
)


def _repository(path: Path) -> TaskRepository:
    connection = sqlite3.connect(path, check_same_thread=False)
    initialize_database(connection)
    return TaskRepository(connection)


def _task(
    task_id: str = "task-1",
    *,
    parent_task_id: str | None = None,
    status: TaskStatus = TaskStatus.ACTIVE,
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        parent_task_id=parent_task_id,
        title="Hermes 长任务交接",
        created_session_id="session-1",
        last_session_id="session-1",
        goal="实现 Hermes Policy 与 SQLite Task State",
        constraints=("不修改 chris-avatar",),
        current_phase="P2",
        completed=("P1 Policy Resolver",),
        in_progress=("SQLite migration",),
        known_issues=(),
        next_actions=("实现 Task Repository",),
        decisions=("新任务默认暂存",),
        artifacts=("HANDOFF.md",),
        status=status,
        search_aliases=("上下文交接", "policy resolver"),
        tags=("Hermes", "SQLite"),
        paused_at=None,
        last_resumed_at=None,
        resume_count=0,
        created_at="2026-08-26T10:00:00+00:00",
        updated_at="2026-08-26T10:00:00+00:00",
        version=0,
    )


def _checkpoint(task_id: str = "task-1") -> CheckpointRecord:
    return CheckpointRecord(
        checkpoint_id="checkpoint-1",
        task_id=task_id,
        session_id="session-1",
        goal="实现 Hermes Policy 与 SQLite Task State",
        constraints=("不修改 chris-avatar",),
        current_phase="P2",
        completed=("P1",),
        current_state=("正在实现 Repository",),
        decisions=("新任务默认暂存",),
        rejected_alternatives=("新任务直接结束旧任务",),
        known_issues=(),
        artifacts=("HANDOFF.md",),
        next_actions=("继续实现 Repository",),
        content_checksum="checksum-1",
        created_at="2026-08-26T10:05:00+00:00",
    )


def test_migration_is_idempotent_and_enables_required_sqlite_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 51, 3))
    database_path = tmp_path / "data.db"
    connection = sqlite3.connect(database_path, check_same_thread=False)

    first_backend = initialize_database(connection)
    second_backend = initialize_database(connection)

    version = connection.execute("PRAGMA user_version").fetchone()[0]
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
        )
    }
    assert version == SCHEMA_VERSION
    assert foreign_keys == 1
    assert journal_mode == "wal"
    assert first_backend == second_backend
    assert {
        "tasks",
        "task_events",
        "checkpoints",
        "context_segments",
        "session_context_state",
    } <= tables


@pytest.mark.parametrize(
    ("sqlite_version", "expected_journal_mode"),
    (
        ((3, 44, 5), "delete"),
        ((3, 44, 6), "wal"),
        ((3, 50, 4), "delete"),
        ((3, 50, 7), "wal"),
        ((3, 51, 2), "delete"),
        ((3, 51, 3), "wal"),
    ),
)
def test_migration_avoids_wal_reset_corruption_versions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sqlite_version: tuple[int, int, int],
    expected_journal_mode: str,
) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", sqlite_version)
    connection = sqlite3.connect(tmp_path / "journal-mode.db")

    initialize_database(connection)

    assert connection.execute("PRAGMA journal_mode").fetchone()[0] == (
        expected_journal_mode
    )


def test_repository_round_trips_all_entities_and_preserves_event_order(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "data.db")
    parent = _task()
    child = _task("task-2", parent_task_id=parent.task_id)

    with repository.transaction():
        repository.create_task(parent)
        repository.create_task(child)
        first_event = repository.append_event(
            task_id=parent.task_id,
            event_type=EventType.TASK_CREATED,
            payload={"goal": parent.goal},
            session_id="session-1",
            created_at="2026-08-26T10:00:00+00:00",
            event_id="event-1",
        )
        second_event = repository.append_event(
            task_id=parent.task_id,
            event_type=EventType.DECISION_MADE,
            payload={"decision": "pause by default"},
            session_id="session-1",
            created_at="2026-08-26T10:01:00+00:00",
            event_id="event-2",
        )
        repository.create_checkpoint(_checkpoint())
        repository.create_segment(
            ContextSegmentRecord(
                context_segment_id="segment-1",
                session_id="session-1",
                task_id=parent.task_id,
                parent_segment_id=None,
                checkpoint_id=None,
                start_message_index=0,
                end_message_index=None,
                start_time="2026-08-26T10:00:00+00:00",
                end_time=None,
                handoff_reason=None,
                handoff_policy_snapshot=None,
                archived_context_reference=None,
            )
        )
        repository.create_session_state(
            SessionContextState(
                session_id="session-1",
                active_task_id=parent.task_id,
                active_context_segment_id="segment-1",
                handoff_pending=False,
                pending_checkpoint_id=None,
                last_handoff_at=None,
                version=0,
            )
        )

    assert repository.get_task(parent.task_id) == parent
    assert repository.get_task(child.task_id) == child
    assert repository.get_checkpoint("checkpoint-1") == _checkpoint()
    assert repository.get_latest_checkpoint(parent.task_id) == _checkpoint()
    assert repository.get_segment("segment-1").task_id == parent.task_id
    assert repository.get_session_state("session-1").active_task_id == parent.task_id
    events = repository.list_events(parent.task_id)
    assert events == (first_event, second_event)
    assert [event.sequence for event in events] == [1, 2]
    repository.close()

    reopened = _repository(tmp_path / "data.db")
    assert reopened.get_session_state("session-1").active_task_id == parent.task_id
    assert reopened.get_segment("segment-1").task_id == parent.task_id


def test_transaction_rolls_back_all_changes_on_failure(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "data.db")

    with pytest.raises(RuntimeError, match="abort"), repository.transaction():
        repository.create_task(_task())
        repository.append_event(
            task_id="task-1",
            event_type=EventType.TASK_CREATED,
            payload={},
            session_id="session-1",
            created_at="2026-08-26T10:00:00+00:00",
            event_id="event-1",
        )
        raise RuntimeError("abort")

    assert repository.get_task("task-1") is None


def test_optimistic_updates_reject_stale_task_and_session_versions(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path / "data.db")
    with repository.transaction():
        repository.create_task(_task())
        repository.create_session_state(
            SessionContextState(
                session_id="session-1",
                active_task_id="task-1",
                active_context_segment_id=None,
                handoff_pending=False,
                pending_checkpoint_id=None,
                last_handoff_at=None,
                version=0,
            )
        )
        updated = repository.update_task(
            "task-1",
            expected_version=0,
            changes={"current_phase": "P2 Repository"},
            updated_at="2026-08-26T10:02:00+00:00",
        )
        state = repository.update_session_state(
            "session-1",
            expected_version=0,
            active_task_id="task-1",
            active_context_segment_id=None,
            handoff_pending=True,
            pending_checkpoint_id=None,
            last_handoff_at=None,
        )

    assert updated.version == 1
    assert state.version == 1
    with pytest.raises(ConcurrentUpdateError), repository.transaction():
        repository.update_task(
            "task-1",
            expected_version=0,
            changes={"current_phase": "stale"},
            updated_at="2026-08-26T10:03:00+00:00",
        )
    with pytest.raises(ConcurrentUpdateError), repository.transaction():
        repository.update_session_state(
            "session-1",
            expected_version=0,
            active_task_id=None,
            active_context_segment_id=None,
            handoff_pending=False,
            pending_checkpoint_id=None,
            last_handoff_at=None,
        )


def test_natural_language_search_indexes_chinese_task_state_and_survives_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "data.db"
    repository = _repository(database_path)
    with repository.transaction():
        repository.create_task(_task(status=TaskStatus.PAUSED))
        repository.create_task(
            TaskRecord(
                **{
                    **_task("task-2", status=TaskStatus.PAUSED).as_dict(),
                    "title": "支付模块超时修复",
                    "goal": "解决 payment API timeout",
                    "search_aliases": ("payment",),
                    "tags": ("backend",),
                }
            )
        )

    results = repository.search_tasks(
        "之前那个 Hermes Policy 任务",
        statuses=(TaskStatus.PAUSED,),
        limit=5,
    )

    assert results
    assert results[0].task.task_id == "task-1"
    repository.close()

    reopened = _repository(database_path)
    assert reopened.get_task("task-1") is not None
    assert (
        reopened.search_tasks(
            "上下文交接",
            statuses=(TaskStatus.PAUSED,),
            limit=5,
        )[0].task.task_id
        == "task-1"
    )


def test_migration_refuses_to_downgrade_a_newer_database(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "future.db")
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")

    with pytest.raises(RuntimeError, match="newer than supported"):
        initialize_database(connection)

    assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION + 1


def test_concurrent_task_updates_allow_exactly_one_version_winner(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "concurrent.db"
    setup = _repository(database_path)
    with setup.transaction():
        setup.create_task(_task())
    setup.close()

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def update(phase: str) -> None:
        repository = _repository(database_path)
        barrier.wait()
        try:
            with repository.transaction():
                repository.update_task(
                    "task-1",
                    expected_version=0,
                    changes={"current_phase": phase},
                    updated_at=f"2026-08-26T10:0{phase[-1]}:00+00:00",
                )
        except ConcurrentUpdateError:
            outcomes.append("stale")
        else:
            outcomes.append("updated")
        finally:
            repository.close()

    threads = [
        threading.Thread(target=update, args=("phase-1",)),
        threading.Thread(target=update, args=("phase-2",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert sorted(outcomes) == ["stale", "updated"]
    final = _repository(database_path).get_task("task-1")
    assert final.version == 1
