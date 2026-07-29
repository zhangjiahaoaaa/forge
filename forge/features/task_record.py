"""Durable task record types for the Loop runtime.

TaskRecord stores long-lived business task state independently of the Harness
Run state.  It deliberately has no Engine or Runtime imports.
"""

from __future__ import annotations

import enum
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


class TaskStatus(str, enum.Enum):
    """PR 1 中 Durable Task 的最小业务状态。"""

    CREATED = "created"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_task_id() -> str:
    return "task_" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


@dataclass
class TaskRecord:
    """一个 Durable Task 的可持久化状态快照。"""

    task_id: str
    goal: str
    status: TaskStatus = TaskStatus.CREATED
    schema_version: int = 1
    revision: int = 1
    created_at: str = ""
    updated_at: str = ""
    current_run_id: str = ""
    run_ids: list[str] = field(default_factory=list)
    last_run_summary: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = _now()
        if not self.updated_at:
            self.updated_at = self.created_at
        if isinstance(self.status, str):
            self.status = TaskStatus(self.status)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status.value,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "current_run_id": self.current_run_id,
            "run_ids": list(self.run_ids),
            "last_run_summary": self.last_run_summary,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TaskRecord":
        return cls(
            task_id=str(data["task_id"]),
            goal=str(data.get("goal", "")),
            status=TaskStatus(data.get("status", "created")),
            schema_version=int(data.get("schema_version", 1)),
            revision=int(data.get("revision", 1)),
            created_at=str(data.get("created_at", "")),
            updated_at=str(data.get("updated_at", "")),
            current_run_id=str(data.get("current_run_id", "")),
            run_ids=list(data.get("run_ids", [])),
            last_run_summary=str(data.get("last_run_summary", "")),
        )


@dataclass
class AttemptRecord:
    """关联到 Durable Task 的一次 Harness Run。"""

    sequence: int
    run_id: str
    legacy_engine_task_id: str = ""
    started_at: str = ""
    finished_at: str = ""
    outcome: str = "unknown"
    summary: str = ""
    artifact_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "run_id": self.run_id,
            "legacy_engine_task_id": self.legacy_engine_task_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "outcome": self.outcome,
            "summary": self.summary,
            "artifact_refs": list(self.artifact_refs),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AttemptRecord":
        return cls(
            sequence=int(data.get("sequence", 0)),
            run_id=str(data.get("run_id", "")),
            legacy_engine_task_id=str(data.get("legacy_engine_task_id", "")),
            started_at=str(data.get("started_at", "")),
            finished_at=str(data.get("finished_at", "")),
            outcome=str(data.get("outcome", "unknown")),
            summary=str(data.get("summary", "")),
            artifact_refs=list(data.get("artifact_refs", [])),
        )


class TaskStore:
    """基于文件系统的 Durable Task 持久化。

    所有读改写操作均使用任务级排他锁；任务状态与 attempt 文件各自
    原子替换，以避免进程中断时留下半个 JSON 文档。
    """

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self._tasks_root = self.root / "tasks"

    def create_task(self, goal: str) -> TaskRecord:
        """创建一个新的长期任务。"""
        task = TaskRecord(task_id=_new_task_id(), goal=goal, status=TaskStatus.CREATED)
        with self._task_lock(task.task_id):
            self._write_task_unlocked(task)
        return task

    def load_task(self, task_id: str) -> TaskRecord:
        """按 durable task_id 读取任务。"""
        with self._task_lock(task_id):
            return self._load_task_unlocked(task_id)

    def save_task(self, task: TaskRecord) -> TaskRecord:
        """原子保存任务并增加 revision。"""
        with self._task_lock(task.task_id):
            current = self._load_task_unlocked(task.task_id)
            # 拒绝过期快照覆盖其他进程的新状态。
            if task.revision != current.revision:
                raise ValueError(f"stale task revision: {task.task_id}")
            task.revision += 1
            task.updated_at = _now()
            self._write_task_unlocked(task)
            return task

    def list_tasks(self) -> list[TaskRecord]:
        """返回所有已知任务，按创建时间倒序排列。"""
        if not self._tasks_root.exists():
            return []
        tasks = []
        for entry in self._tasks_root.iterdir():
            if not entry.is_dir() or entry.name == ".locks":
                continue
            try:
                tasks.append(self.load_task(entry.name))
            except (json.JSONDecodeError, KeyError, OSError, ValueError):
                continue
        return sorted(tasks, key=lambda item: item.created_at, reverse=True)

    def close_task(self, task_id: str, status: TaskStatus, reason: str = "") -> TaskRecord:
        """由人显式关闭任务，并保存审计原因。"""
        if status not in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            raise ValueError(f"terminal status required: {status}")
        with self._task_lock(task_id):
            task = self._load_task_unlocked(task_id)
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                raise ValueError(f"task already in terminal state: {task.status}")
            if self._active_attempt_unlocked(task_id):
                raise ValueError(f"cannot close task with active run: {task.current_run_id}")
            task.status = status
            if reason:
                task.last_run_summary = reason
            self._touch_and_write_unlocked(task)
            return task

    def record_run_started(self, task_id: str, run_id: str, legacy_engine_task_id: str = "") -> TaskRecord:
        """在 Harness Run 执行前持久化其关联，且同一 run_id 幂等。"""
        if not str(run_id).strip():
            raise ValueError("run_id is required")
        with self._task_lock(task_id):
            task = self._load_task_unlocked(task_id)
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                raise ValueError(f"cannot start run for terminal task: {task.status}")
            active = self._active_attempt_unlocked(task_id)
            if active and active.run_id != run_id:
                raise ValueError(f"task already has an active run: {active.run_id}")

            attempts = self._read_attempts_unlocked(task_id)
            existing = next((item for item in attempts if item.run_id == run_id), None)
            if existing and existing.finished_at:
                raise ValueError(f"run already finished: {run_id}")

            # 先写 task.json：该文件是崩溃恢复的权威入口。若进程在随后
            # 写入 attempts.jsonl 前中断，recover_active_run() 仍能定位此 Run。
            task_changed = False
            if run_id not in task.run_ids:
                task.run_ids.append(run_id)
                task_changed = True
            if task.current_run_id != run_id:
                task.current_run_id = run_id
                task_changed = True
            if task.status != TaskStatus.IN_PROGRESS:
                task.status = TaskStatus.IN_PROGRESS
                task_changed = True
            if task_changed:
                self._touch_and_write_unlocked(task)

            if existing is None:
                attempts.append(AttemptRecord(
                    sequence=len(attempts) + 1,
                    run_id=run_id,
                    legacy_engine_task_id=legacy_engine_task_id,
                    started_at=_now(),
                    outcome="running",
                ))
                self._write_attempts_unlocked(task_id, attempts)
            return task

    def record_run_finished(self, task_id: str, run_id: str, outcome: str = "completed", summary: str = "", artifact_refs: list[str] | None = None) -> TaskRecord:
        """完成 Run；同一 run_id 重复提交不会覆盖已落盘结果。"""
        with self._task_lock(task_id):
            task = self._load_task_unlocked(task_id)
            attempts = self._read_attempts_unlocked(task_id)
            attempt = next((item for item in attempts if item.run_id == run_id), None)
            if attempt is None:
                # 恢复 task.json 已关联、attempt 写入前崩溃的极小窗口。
                if run_id not in task.run_ids:
                    raise KeyError(f"run not associated with task: {run_id}")
                attempt = AttemptRecord(sequence=len(attempts) + 1, run_id=run_id, started_at=_now())
                attempts.append(attempt)
            if attempt.finished_at:
                return task
            attempt.finished_at = _now()
            attempt.outcome = outcome
            attempt.summary = summary
            if artifact_refs:
                attempt.artifact_refs = list(artifact_refs)
            self._write_attempts_unlocked(task_id, attempts)
            task.last_run_summary = summary
            self._touch_and_write_unlocked(task)
            return task

    def list_attempts(self, task_id: str) -> list[AttemptRecord]:
        """返回任务的完整 attempt 历史。"""
        with self._task_lock(task_id):
            return self._read_attempts_unlocked(task_id)

    def recover_active_run(self, task_id: str) -> AttemptRecord | None:
        """返回重启后可供上层 Resume 决策的未结束 Run，不改变其状态。"""
        with self._task_lock(task_id):
            task = self._load_task_unlocked(task_id)
            active = self._active_attempt_unlocked(task_id)
            if active:
                return active
            # task.json 先落盘、attempt 尚未写入就崩溃时仍可定位 Run。
            if task.current_run_id and task.current_run_id in task.run_ids:
                return AttemptRecord(sequence=len(task.run_ids), run_id=task.current_run_id, outcome="running")
            return None

    def _active_attempt_unlocked(self, task_id: str) -> AttemptRecord | None:
        return next((item for item in reversed(self._read_attempts_unlocked(task_id)) if not item.finished_at), None)

    def _task_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "task.json"

    def _task_dir(self, task_id: str) -> Path:
        return self._tasks_root / _safe_segment(task_id)

    def _attempts_path(self, task_id: str) -> Path:
        return self._task_dir(task_id) / "attempts.jsonl"

    def _load_task_unlocked(self, task_id: str) -> TaskRecord:
        path = self._task_path(task_id)
        if not path.exists():
            raise KeyError(f"task not found: {task_id}")
        return TaskRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _touch_and_write_unlocked(self, task: TaskRecord) -> None:
        task.revision += 1
        task.updated_at = _now()
        self._write_task_unlocked(task)

    def _write_task_unlocked(self, task: TaskRecord) -> None:
        self._task_dir(task.task_id).mkdir(parents=True, exist_ok=True)
        self._atomic_write(self._task_path(task.task_id), json.dumps(task.to_dict(), indent=2, ensure_ascii=False) + "\n")

    def _read_attempts_unlocked(self, task_id: str) -> list[AttemptRecord]:
        path = self._attempts_path(task_id)
        if not path.exists():
            return []
        records = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                if line.strip():
                    records.append(AttemptRecord.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
        return records

    def _write_attempts_unlocked(self, task_id: str, records: list[AttemptRecord]) -> None:
        self._task_dir(task_id).mkdir(parents=True, exist_ok=True)
        payload = "".join(json.dumps(item.to_dict(), sort_keys=True, ensure_ascii=False) + "\n" for item in records)
        self._atomic_write(self._attempts_path(task_id), payload)

    @staticmethod
    def _atomic_write(path: Path, payload: str) -> None:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent), prefix=path.name + ".", suffix=".tmp") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            tmp_name = handle.name
        Path(tmp_name).replace(path)

    def _task_lock(self, task_id: str) -> "_TaskLock":
        return _TaskLock(self._tasks_root / ".locks" / (_safe_segment(task_id) + ".lock"))


class _TaskLock:
    """跨进程任务锁，Windows 与 POSIX 均使用标准库实现。"""

    def __init__(self, path: Path, timeout_seconds: float = 10.0):
        self._path = path
        self._timeout_seconds = timeout_seconds
        self._handle = None

    def __enter__(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a+b")
        deadline = time.monotonic() + self._timeout_seconds
        while True:
            try:
                self._acquire()
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise TimeoutError(f"timed out acquiring task lock: {self._path}")
                time.sleep(0.02)

    def __exit__(self, *exc):
        if self._handle is not None:
            self._release()
            self._handle.close()
            self._handle = None

    def _acquire(self) -> None:
        if os.name == "nt":
            import msvcrt
            self._handle.seek(0)
            if self._handle.tell() == 0:
                self._handle.write(b"0")
                self._handle.flush()
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _release(self) -> None:
        if os.name == "nt":
            import msvcrt
            self._handle.seek(0)
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)


def _safe_segment(task_id: str) -> str:
    """确保 task_id 只能作为当前 tasks 根目录下的普通段。"""
    value = str(task_id or "").strip()
    if not value or value in {".", ".."}:
        raise ValueError(f"invalid task_id: {task_id}")
    return value.replace("/", "_").replace("\\", "_")
