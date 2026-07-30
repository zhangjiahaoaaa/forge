"""Durable task record types for the Loop runtime.

TaskRecord stores long-lived business task state independently of the Harness
Run state.  It deliberately has no Engine or Runtime imports.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class TaskStatus(str, enum.Enum):
    """Durable Task 的业务状态（PR 1 – PR 3）。"""

    CREATED = "created"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    WAITING_HUMAN = "waiting_human"


class TaskPhase(str, enum.Enum):
    """当前执行阶段，与业务状态正交。

    status 回答"业务上怎么了"，phase 回答"流程走到哪了"。
    一个 in_progress 的任务可以处于不同的 phase。
    只有 RUNNING 和 VERIFYING 涉及 Agent 调用。
    """

    BASELINING = "baselining"
    READY = "ready"
    RUNNING = "running"
    VERIFYING = "verifying"
    ASSESSING_PROGRESS = "assessing_progress"
    REPLANNING = "replanning"
    RUN_INTERRUPTED = "run_interrupted"


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

    # PR 2 — Acceptance Contract + External Verifier
    baseline: dict | None = None
    """基线信息（reproduce 结果、commit、failure_fingerprint）。"""
    last_verdict: dict | None = None
    """最近一次验收裁决。"""
    completion_reason: str | None = None
    """completed 或 blocked 的具体原因（如 already_resolved, baseline_mismatch）。"""

    # PR 3 — Loop Controller
    phase: str = ""
    """当前执行阶段（TaskPhase 值），空字符串表示尚未进入循环。"""
    cycle: int = 0
    """业务修复轮次，只在启动新 Agent 尝试时增加。"""
    max_cycles: int = 4
    """允许的最大业务轮次。"""
    policy: dict | None = None
    """Loop Policy 配置（max_tool_steps, max_wall_time 等）。"""
    contract_hash: str = ""
    """Runtime 冻结 Contract 时记录的可信内容哈希。"""
    replan_already_done: bool = False
    """是否已执行过一次 REPLAN，重启后仍保留。"""
    progress_history: list[dict] = field(default_factory=list)
    """最近的停滞检测信号，跨 CLI 进程保留。"""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = _now()
        if not self.updated_at:
            self.updated_at = self.created_at
        if isinstance(self.status, str):
            self.status = TaskStatus(self.status)

    def to_dict(self) -> dict:
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status.value if isinstance(self.status, enum.Enum) else str(self.status),
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "current_run_id": self.current_run_id,
            "run_ids": list(self.run_ids),
            "last_run_summary": self.last_run_summary,
        }
        if self.baseline:
            d["baseline"] = dict(self.baseline)
        if self.last_verdict:
            d["last_verdict"] = dict(self.last_verdict)
        if self.completion_reason:
            d["completion_reason"] = self.completion_reason
        if self.contract_hash:
            d["contract_hash"] = self.contract_hash
        if self.phase:
            d["phase"] = self.phase
        if self.cycle:
            d["cycle"] = self.cycle
        if self.max_cycles != 4:
            d["max_cycles"] = self.max_cycles
        if self.policy:
            d["policy"] = dict(self.policy)
        if self.replan_already_done:
            d["replan_already_done"] = True
        if self.progress_history:
            d["progress_history"] = [dict(item) for item in self.progress_history]
        return d

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
            baseline=dict(data["baseline"]) if data.get("baseline") else None,
            last_verdict=dict(data["last_verdict"]) if data.get("last_verdict") else None,
            completion_reason=str(data["completion_reason"]) if data.get("completion_reason") else None,
            contract_hash=str(data.get("contract_hash", "")),
            phase=str(data.get("phase", "")),
            cycle=int(data.get("cycle", 0)),
            max_cycles=int(data.get("max_cycles", 4)),
            policy=dict(data["policy"]) if data.get("policy") else None,
            replan_already_done=bool(data.get("replan_already_done", False)),
            progress_history=[dict(item) for item in data.get("progress_history", []) if isinstance(item, dict)],
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
    tool_steps: int = 0
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
            "tool_steps": self.tool_steps,
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
            tool_steps=int(data.get("tool_steps", 0)),
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
            task.completion_reason = "human_closed" if status == TaskStatus.COMPLETED else "human_failed"
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

            # 记录 Run 开始前的 task.json 内容哈希，用于结束后完整性校验
            self._set_run_guard(task_id, run_id)
            return task

    def record_run_finished(self, task_id: str, run_id: str, outcome: str = "completed",
                            summary: str = "", tool_steps: int = 0,
                            artifact_refs: list[str] | None = None) -> TaskRecord:
        """完成 Run；同一 run_id 重复提交不会覆盖已落盘结果。

        完成后清空 current_run_id，防止 recover_active_run() 误恢复。
        """
        with self._task_lock(task_id):
            task = self._load_task_unlocked(task_id)
            attempts = self._read_attempts_unlocked(task_id)
            attempt = next((item for item in attempts if item.run_id == run_id), None)
            if attempt is None:
                if run_id not in task.run_ids:
                    raise KeyError(f"run not associated with task: {run_id}")
                attempt = AttemptRecord(sequence=len(attempts) + 1, run_id=run_id, started_at=_now())
                attempts.append(attempt)
            if attempt.finished_at:
                return task
            attempt.finished_at = _now()
            attempt.outcome = outcome
            attempt.summary = summary
            attempt.tool_steps = tool_steps
            if artifact_refs:
                attempt.artifact_refs = list(artifact_refs)
            self._write_attempts_unlocked(task_id, attempts)
            task.last_run_summary = summary

            # 在修改 task.json 之前校验 Run 完整性。
            if run_id and not self.verify_run_integrity(task_id, run_id):
                task.completion_reason = "integrity_violation"
                task.status = TaskStatus.FAILED

            # 无论结果如何都结束 attempt；FAILED 任务也不能残留 active run。
            task.current_run_id = ""
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
            # 注意：仅当 current_run_id 非空且对应 attempt 确实不存在时才返回。
            if task.current_run_id and task.current_run_id in task.run_ids:
                attempts = self._read_attempts_unlocked(task_id)
                if not any(a.run_id == task.current_run_id and not a.finished_at for a in attempts):
                    return AttemptRecord(sequence=len(task.run_ids), run_id=task.current_run_id, outcome="running")
            return None

    def _active_attempt_unlocked(self, task_id: str) -> AttemptRecord | None:
        return next((item for item in reversed(self._read_attempts_unlocked(task_id)) if not item.finished_at), None)

    # -- Run 完整性校验：检测 Run 期间 task.json 是否被外部篡改 -------

    def _set_run_guard(self, task_id: str, run_id: str) -> None:
        """记录 Run 开始时 task.json 的 checksum，用于结束后校验。"""
        guard_dir = self._task_dir(task_id) / ".guards"
        guard_dir.mkdir(parents=True, exist_ok=True)
        guard_path = guard_dir / f"{run_id}.sha256"
        try:
            task_bytes = self._task_path(task_id).read_bytes()
            guard_path.write_text(hashlib.sha256(task_bytes).hexdigest(), encoding="utf-8")
        except OSError:
            pass

    def verify_run_integrity(self, task_id: str, run_id: str) -> bool:
        """检查 Run 期间 task.json 是否被非 Runtime 写入修改。"""
        guard_dir = self._task_dir(task_id) / ".guards"
        guard_path = guard_dir / f"{run_id}.sha256"
        if not guard_path.exists():
            return True  # 无 guard 文件时默认通过
        try:
            expected = guard_path.read_text(encoding="utf-8").strip()
            actual = hashlib.sha256(self._task_path(task_id).read_bytes()).hexdigest()
            guard_path.unlink(missing_ok=True)
            return expected == actual
        except OSError:
            return True

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
