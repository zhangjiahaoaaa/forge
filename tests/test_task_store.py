"""PR 1 Durable Task Layer 的确定性测试。"""

import json
import os

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


def test_integrity_failure_closes_current_run(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task("tampered")
    store.record_run_started(task.task_id, "run_101")
    task_path = tmp_path / "tasks" / task.task_id / "task.json"
    tampered = json.loads(task_path.read_text(encoding="utf-8"))
    tampered["goal"] = "tampered"
    task_path.write_text(json.dumps(tampered), encoding="utf-8")

    updated = store.record_run_finished(task.task_id, "run_101", outcome="failed")

    assert updated.status == TaskStatus.FAILED
    assert updated.current_run_id == ""
    assert store.recover_active_run(task.task_id) is None


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
    original = AttemptRecord(sequence=1, run_id="run_1", legacy_engine_task_id="legacy_1", started_at="2026-01-01T00:00:00", finished_at="2026-01-01T01:00:00", outcome="completed", summary="fixed", tool_steps=3, artifact_refs=["runs/run_1/report.json"])
    restored = AttemptRecord.from_dict(original.to_dict())
    assert restored.sequence == 1
    assert restored.run_id == "run_1"
    assert restored.legacy_engine_task_id == "legacy_1"
    assert restored.outcome == "completed"
    assert restored.summary == "fixed"
    assert restored.tool_steps == 3
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


def test_update_attempt_persists_mid_run_fields(tmp_path):
    """运行中落盘 session / checkpoint / identity，崩溃后仍可恢复。"""
    store = TaskStore(tmp_path)
    task = store.create_task("mid-run fields")
    store.record_run_started(task.task_id, "run_1")
    store.update_attempt(
        task.task_id, "run_1",
        session_id="session_abc",
        checkpoint_ref="ckpt_7",
        runtime_identity={"model": "m", "tool_signature": "sig"},
    )
    attempt = store.list_attempts(task.task_id)[0]
    assert attempt.session_id == "session_abc"
    assert attempt.checkpoint_ref == "ckpt_7"
    assert attempt.runtime_identity == {"model": "m", "tool_signature": "sig"}
    assert attempt.outcome == "running"  # 仍是 active attempt


def test_update_attempt_skips_empty_values_and_missing_run(tmp_path):
    store = TaskStore(tmp_path)
    task = store.create_task("skip empty")
    store.record_run_started(task.task_id, "run_1")
    store.update_attempt(task.task_id, "run_1", session_id="", checkpoint_ref="", tool_steps=0)
    attempt = store.list_attempts(task.task_id)[0]
    assert attempt.session_id == ""
    assert attempt.checkpoint_ref == ""
    try:
        store.update_attempt(task.task_id, "run_ghost", session_id="s")
        assert False, "expected KeyError for unknown run"
    except KeyError:
        pass


def test_record_run_interrupted_persists_tool_steps(tmp_path):
    """中断 Run 记录 tool_steps，恢复后总预算不被绕过。"""
    store = TaskStore(tmp_path)
    task = store.create_task("interrupted budget")
    store.record_run_started(task.task_id, "run_1")
    store.record_run_interrupted(
        task.task_id, "run_1",
        summary="crash", tool_steps=5, checkpoint_ref="ckpt_3",
    )
    attempt = store.list_attempts(task.task_id)[0]
    assert attempt.outcome == "interrupted"
    assert attempt.tool_steps == 5
    assert attempt.checkpoint_ref == "ckpt_3"
    assert attempt.finished_at is not None
    # 中断后 recover_active_run 不再返回该 Run
    assert store.recover_active_run(task.task_id) is None


def test_attempt_owner_pid_roundtrip(tmp_path):
    """record_run_started 记录 owner PID，重启后仍可读取（recover 所有权判定）。"""
    store = TaskStore(tmp_path)
    task = store.create_task("owner pid")
    store.record_run_started(task.task_id, "run_1")
    attempt = store.list_attempts(task.task_id)[0]
    assert attempt.owner_pid == os.getpid()
    assert attempt.owner_started_at
    # 模拟进程重启：新 store 实例读取同一文件
    attempt2 = TaskStore(tmp_path).list_attempts(task.task_id)[0]
    assert attempt2.owner_pid == os.getpid()
    assert attempt2.owner_started_at == attempt.owner_started_at


def test_record_run_started_rejects_live_owner_reentry(tmp_path):
    """同 run_id 幂等重入受所有权约束：旧 owner（其他存活进程）→ 拒绝。"""
    import subprocess
    import sys

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(10)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        store = TaskStore(tmp_path)
        task = store.create_task("owner reentry")
        store.record_run_started(task.task_id, "run_1")
        # 模拟另一进程持有：改 owner 为活着的子进程
        attempts = store.list_attempts(task.task_id)
        attempts[0].owner_pid = child.pid
        attempts[0].owner_started_at = ""
        store._write_attempts_unlocked(task.task_id, attempts)
        try:
            store.record_run_started(task.task_id, "run_1")
            assert False, "expected live-owner rejection"
        except ValueError as exc:
            assert "live process" in str(exc), str(exc)
        # 子进程退出后（owner 失效）→ 幂等重入放行并更新 owner
        child.kill()
        child.wait(timeout=10)
        store.record_run_started(task.task_id, "run_1")
        attempt = store.list_attempts(task.task_id)[0]
        assert attempt.owner_pid == os.getpid(), "reentry must reclaim ownership"
    finally:
        if child.poll() is None:
            child.kill()


def test_claim_active_run_updates_owner_atomically(tmp_path):
    """claim_active_run：旧 owner 失效才允许认领；旧 owner 存活时拒绝。"""
    import os

    store = TaskStore(tmp_path)
    task = store.create_task("claim")
    store.record_run_started(task.task_id, "run_1")
    # 模拟崩溃 owner（死 PID）→ 可认领
    attempts = store.list_attempts(task.task_id)
    attempts[0].owner_pid = 99999999
    attempts[0].owner_started_at = ""
    store._write_attempts_unlocked(task.task_id, attempts)
    claimed = store.claim_active_run(task.task_id, "run_1",
                                     owner_pid=os.getpid(),
                                     owner_started_at="2026-01-01T00:00:00+00:00")
    assert claimed.owner_pid == os.getpid()
    assert claimed.owner_started_at == "2026-01-01T00:00:00+00:00"

    # owner 存活（当前进程）→ 拒绝认领
    task2 = store.create_task("claim2")
    store.record_run_started(task2.task_id, "run_2")
    try:
        store.claim_active_run(task2.task_id, "run_2", owner_pid=99999999)
        assert False, "expected live-owner rejection"
    except ValueError as exc:
        assert "live process" in str(exc), str(exc)


# ---------------------------------------------------------------------------
# 跨进程锁
# ---------------------------------------------------------------------------

def _lock_worker(root: str, task_id: str, run_id: str, result_queue):
    """子进程：尝试为 task 启动 run，把结果放入队列。"""
    from forge.features.task_record import TaskStore
    try:
        store = TaskStore(root)
        store.record_run_started(task_id, run_id)
        result_queue.put(("ok", run_id))
    except Exception as exc:
        result_queue.put(("error", str(exc)))


def test_task_lock_is_cross_process_exclusive(tmp_path):
    """两个独立进程同时 record_run_started，仅一方成功。

    Windows 覆盖 msvcrt.locking 分支，POSIX 覆盖 fcntl.flock 分支。
    使用 spawn 上下文保证子进程不继承父进程句柄。
    """
    import multiprocessing as mp

    store = TaskStore(tmp_path)
    task = store.create_task("cross process lock")

    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    p1 = ctx.Process(target=_lock_worker, args=(str(tmp_path), task.task_id, "run_a", queue))
    p2 = ctx.Process(target=_lock_worker, args=(str(tmp_path), task.task_id, "run_b", queue))
    p1.start()
    p2.start()
    p1.join(30)
    p2.join(30)
    assert p1.exitcode == 0, f"proc1 exitcode={p1.exitcode}"
    assert p2.exitcode == 0, f"proc2 exitcode={p2.exitcode}"

    results = [queue.get(timeout=10), queue.get(timeout=10)]
    ok = [r for r in results if r[0] == "ok"]
    errors = [r for r in results if r[0] == "error"]

    assert len(ok) == 1, f"expected exactly one winner, got {results}"
    assert len(errors) == 1, f"expected exactly one rejection, got {results}"
    # 拒绝原因必须是 active run
    assert "active run" in errors[0][1], f"unexpected rejection reason: {errors[0][1]}"
    # 获胜的 run 是唯一被记录的
    loaded = store.load_task(task.task_id)
    assert loaded.run_ids == [ok[0][1]], f"run_ids={loaded.run_ids}"
