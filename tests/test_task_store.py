"""PR 1 Durable Task Layer 的确定性测试。"""

from forge.core.run_result import RunResult
from forge.features.task_record import AttemptRecord, TaskRecord, TaskStatus, TaskStore


def test_task_record_creates_with_expected_defaults():
    record = TaskRecord(task_id="t1", goal="fix bug")
    assert record.task_id == "t1"
    assert record.goal == "fix bug"
    assert record.status == TaskStatus.CREATED
    assert record.revision == 1
    assert record.run_ids == []
    assert record.current_run_id == ""
    assert record.last_run_summary == ""


def test_task_record_serialize_roundtrip():
    original = TaskRecord(task_id="t1", goal="fix bug", status=TaskStatus.IN_PROGRESS, revision=3, current_run_id="run_101", run_ids=["run_100", "run_101"], last_run_summary="fixed the lock issue")
    restored = TaskRecord.from_dict(original.to_dict())
    assert restored.task_id == original.task_id
    assert restored.goal == original.goal
    assert restored.status == original.status
    assert restored.revision == original.revision
    assert restored.current_run_id == original.current_run_id
    assert restored.run_ids == original.run_ids
    assert restored.last_run_summary == original.last_run_summary


def test_task_record_default_status_is_created():
    assert TaskRecord.from_dict({"task_id": "t1", "goal": "x"}).status == TaskStatus.CREATED


def test_task_store_create_and_load(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task("my goal")
    assert task.task_id
    assert task.goal == "my goal"
    assert task.status == TaskStatus.CREATED
    loaded = store.load_task(task.task_id)
    assert loaded.task_id == task.task_id
    assert loaded.goal == task.goal


def test_task_store_list_tasks_empty(tmp_path):
    assert TaskStore(tmp_path).list_tasks() == []


def test_task_store_list_tasks(tmp_path):
    store = TaskStore(tmp_path)
    t1 = store.create_task("first")
    t2 = store.create_task("second")
    task_ids = {task.task_id for task in store.list_tasks()}
    assert {t1.task_id, t2.task_id} <= task_ids


def test_task_store_load_unknown_raises(tmp_path):
    try:
        TaskStore(tmp_path).load_task("nonexistent")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_task_store_save_updates_revision(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task("test")
    task.last_run_summary = "updated"
    saved = store.save_task(task)
    assert saved.revision == 2
    assert store.load_task(task.task_id).last_run_summary == "updated"


def test_task_store_close(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task("close me")
    closed = store.close_task(task.task_id, TaskStatus.COMPLETED, reason="done")
    assert closed.status == TaskStatus.COMPLETED
    assert closed.last_run_summary == "done"


def test_task_store_close_already_terminal_raises(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task("close me")
    store.close_task(task.task_id, TaskStatus.FAILED)
    try:
        store.close_task(task.task_id, TaskStatus.COMPLETED)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_record_run_started(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task("test run")
    updated = store.record_run_started(task.task_id, "run_101", "legacy_1")
    assert updated.status == TaskStatus.IN_PROGRESS
    assert updated.current_run_id == "run_101"
    assert "run_101" in updated.run_ids
    attempts = store.list_attempts(task.task_id)
    assert len(attempts) == 1
    assert attempts[0].run_id == "run_101"
    assert attempts[0].legacy_engine_task_id == "legacy_1"
    assert attempts[0].outcome == "running"


def test_record_run_finished(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task("test run")
    store.record_run_started(task.task_id, "run_101", "legacy_1")
    updated = store.record_run_finished(task.task_id, "run_101", outcome="completed", summary="fix applied")
    assert updated.last_run_summary == "fix applied"
    attempt = store.list_attempts(task.task_id)[0]
    assert attempt.outcome == "completed"
    assert attempt.finished_at


def test_record_run_finished_idempotent(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task("test")
    store.record_run_started(task.task_id, "run_101")
    store.record_run_finished(task.task_id, "run_101", summary="done")
    store.record_run_finished(task.task_id, "run_101", summary="done")
    assert len(store.list_attempts(task.task_id)) == 1


def test_multiple_runs_per_task(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task("multi run")
    for i in range(3):
        run_id = f"run_{i + 1}"
        store.record_run_started(task.task_id, run_id)
        store.record_run_finished(task.task_id, run_id)
    loaded = store.load_task(task.task_id)
    assert len(loaded.run_ids) == 3
    attempts = store.list_attempts(task.task_id)
    assert [attempt.run_id for attempt in attempts] == ["run_1", "run_2", "run_3"]


def test_attempt_record_roundtrip():
    original = AttemptRecord(sequence=1, run_id="run_1", legacy_engine_task_id="legacy_1", started_at="2026-01-01T00:00:00", finished_at="2026-01-01T01:00:00", outcome="completed", summary="fixed", artifact_refs=["runs/run_1/report.json"])
    restored = AttemptRecord.from_dict(original.to_dict())
    assert restored.sequence == 1
    assert restored.run_id == "run_1"
    assert restored.legacy_engine_task_id == "legacy_1"
    assert restored.outcome == "completed"
    assert restored.summary == "fixed"
    assert restored.artifact_refs == ["runs/run_1/report.json"]


class _FakeTaskState:
    def __init__(self, status="completed", stop_reason="final_answer_returned", final_answer="done", run_id="run_1", task_id="legacy_1"):
        self.status = status
        self.stop_reason = stop_reason
        self.final_answer = final_answer
        self.run_id = run_id
        self.task_id = task_id


def test_run_result_from_completed_task_state():
    result = RunResult.from_task_state(_FakeTaskState())
    assert result.final_answer == "done"
    assert result.run_id == "run_1"
    assert result.legacy_engine_task_id == "legacy_1"
    assert result.outcome == "completed"


def test_run_result_handles_stopped_state():
    assert RunResult.from_task_state(_FakeTaskState(status="stopped", stop_reason="step_limit_reached")).outcome == "stopped"


def test_run_result_handles_failed_state():
    assert RunResult.from_task_state(_FakeTaskState(status="failed", stop_reason="model_error")).outcome == "failed"


def test_task_survives_directory_reload(tmp_path):
    store1 = TaskStore(tmp_path)
    task = store1.create_task("persist test")
    store1.record_run_started(task.task_id, "run_1")
    store1.record_run_finished(task.task_id, "run_1", summary="done")
    store2 = TaskStore(tmp_path)
    assert store2.load_task(task.task_id).goal == "persist test"
    assert store2.list_attempts(task.task_id)[0].outcome == "completed"


def test_create_task_with_empty_goal(tmp_path):
    assert TaskStore(tmp_path).create_task("").goal == ""


def test_load_nonexistent_task_id(tmp_path):
    try:
        TaskStore(tmp_path).load_task("nonexistent_task_12345")
        assert False, "expected KeyError"
    except KeyError:
        pass


def test_list_attempts_no_task(tmp_path):
    assert TaskStore(tmp_path).list_attempts("nonexistent") == []


# PR 1 崩溃恢复与并发语义

def test_record_run_started_is_idempotent_for_same_active_run(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task("idempotent start")
    store.record_run_started(task.task_id, "run_1", "legacy_1")
    store.record_run_started(task.task_id, "run_1", "legacy_1")
    assert store.load_task(task.task_id).run_ids == ["run_1"]
    assert len(store.list_attempts(task.task_id)) == 1


def test_record_run_started_rejects_another_active_run(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task("single active run")
    store.record_run_started(task.task_id, "run_1")
    try:
        store.record_run_started(task.task_id, "run_2")
        assert False, "expected active Run rejection"
    except ValueError as exc:
        assert "active run" in str(exc)
    assert store.load_task(task.task_id).run_ids == ["run_1"]


def test_recover_active_run_survives_store_restart(tmp_path):
    store1 = TaskStore(tmp_path)
    task = store1.create_task("recover after crash")
    store1.record_run_started(task.task_id, "run_interrupted", "legacy_run")
    recovered = TaskStore(tmp_path).recover_active_run(task.task_id)
    assert recovered is not None
    assert recovered.run_id == "run_interrupted"
    assert recovered.outcome == "running"


def test_recover_active_run_handles_task_written_before_attempt(tmp_path):
    """模拟 task.json 写完、attempts.jsonl 写入前的崩溃窗口。"""
    store = TaskStore(tmp_path)
    task = store.create_task("recover partial start")
    task.current_run_id = "run_partial"
    task.run_ids.append("run_partial")
    task.status = TaskStatus.IN_PROGRESS
    store._write_task_unlocked(task)

    recovered = TaskStore(tmp_path).recover_active_run(task.task_id)

    assert recovered is not None
    assert recovered.run_id == "run_partial"
    assert recovered.outcome == "running"


def test_close_rejects_non_terminal_status_and_active_run(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task("close validation")
    try:
        store.close_task(task.task_id, TaskStatus.IN_PROGRESS)
        assert False, "expected terminal status validation"
    except ValueError:
        pass
    store.record_run_started(task.task_id, "run_1")
    try:
        store.close_task(task.task_id, TaskStatus.COMPLETED)
        assert False, "expected active Run validation"
    except ValueError as exc:
        assert "active run" in str(exc)
