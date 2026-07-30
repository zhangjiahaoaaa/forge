"""运行 shell 时保护 Runtime 管理的 Durable Task 状态。"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path


class ProtectedTaskStateModified(RuntimeError):
    """表示 shell 修改了 Runtime 管理的 Durable Task 状态。"""

    def __init__(self, paths: list[str]):
        super().__init__("protected Durable Task state modified")
        self.paths = paths


def execute_with_task_state_guard(tool, args, workspace_root: Path):
    """执行 shell 工具，并将受保护状态篡改转换为专用异常。"""
    snapshot = ProtectedTaskStateSnapshot.for_workspace(workspace_root)
    try:
        try:
            result = tool.execute(args).content
        except Exception:
            changed_paths = snapshot.restore_if_changed()
            if changed_paths:
                raise ProtectedTaskStateModified(changed_paths) from None
            raise
        changed_paths = snapshot.restore_if_changed()
        if changed_paths:
            raise ProtectedTaskStateModified(changed_paths)
        return result
    finally:
        snapshot.close()


def reject_protected_task_state_modification(agent, tool, paths: list[str]) -> str:
    """记录拒绝事件，并返回给 Agent 的明确安全错误。"""
    affected_paths = [f".forge/tasks/{path}" for path in paths]
    agent.session_event_bus.emit(
        "protected_task_state_violation",
        {
            "tool_name": tool.name,
            "reason": "protected_task_state_modified",
            "security_event_type": "contract_path_guard",
            "paths": affected_paths,
        },
    )
    agent._last_tool_result_metadata = {
        "tool_status": "rejected",
        "tool_error_code": "protected_task_state_modified",
        "security_event_type": "contract_path_guard",
        "risk_level": "high",
        "read_only": tool.read_only,
        "affected_paths": affected_paths,
        "workspace_changed": False,
        "diff_summary": [],
        "full_output_artifact": "",
    }
    agent.record_process_note_for_tool(tool.name, agent._last_tool_result_metadata)
    return "error: run_shell modified protected Durable Task state; changes were rolled back"


class ProtectedTaskStateSnapshot:
    """保存 ``.forge/tasks`` 快照，并在 shell 越权修改后回滚。

    shell 命令是图灵完备的，不能把命令文本正则当作写入隔离边界。该快照
    在每次 ``run_shell`` 前创建，运行后比较整个 tasks 树并在检测到变化时
    恢复原状。它是未启用 OS sandbox 时的 fail-closed 最后一层保护。
    """

    def __init__(self, forge_root: Path):
        self.forge_root = Path(forge_root)
        self.tasks_root = self.forge_root / "tasks"
        self._tempdir = tempfile.TemporaryDirectory(prefix="forge-task-state-")
        self._backup = Path(self._tempdir.name) / "tasks"
        self._forge_kind = _path_kind(self.forge_root)
        self._tasks_fingerprint = _tree_fingerprint(self.tasks_root)
        if self._tasks_fingerprint is not None:
            shutil.copytree(self.tasks_root, self._backup, symlinks=True)

    @classmethod
    def for_workspace(cls, workspace_root: Path) -> "ProtectedTaskStateSnapshot":
        return cls(Path(workspace_root) / ".forge")

    def restore_if_changed(self) -> list[str]:
        """检测受保护状态变化；发现变化即恢复并返回受影响路径。"""
        current_forge_kind = _path_kind(self.forge_root)
        current = _tree_fingerprint(self.tasks_root)
        if current_forge_kind == self._forge_kind and current == self._tasks_fingerprint:
            return []

        changed_paths = sorted(set(_tree_paths(self.tasks_root)) | set(_tree_paths(self._backup)))
        self._restore()
        return changed_paths or [".forge/tasks"]

    def close(self) -> None:
        self._tempdir.cleanup()

    def _restore(self) -> None:
        # 若 shell 将 .forge 替换成链接或普通文件，绝不沿链接递归删除。
        current_kind = _path_kind(self.forge_root)
        if current_kind not in {"absent", "dir"}:
            self.forge_root.unlink()
        self.forge_root.mkdir(parents=True, exist_ok=True)
        _remove_path(self.tasks_root)
        if self._tasks_fingerprint is not None:
            shutil.copytree(self._backup, self.tasks_root, symlinks=True)


def _path_kind(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "absent"
    if stat.S_ISLNK(mode):
        return "link"
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _tree_fingerprint(root: Path) -> dict[str, str] | None:
    if _path_kind(root) == "absent":
        return None
    fingerprint: dict[str, str] = {".": _path_kind(root)}
    _visit_tree(root, root, fingerprint)
    return fingerprint


def _tree_paths(root: Path) -> list[str]:
    fingerprint = _tree_fingerprint(root)
    return list(fingerprint or ())


def _visit_tree(root: Path, path: Path, fingerprint: dict[str, str]) -> None:
    if _path_kind(path) != "dir":
        return
    with os.scandir(path) as entries:
        for entry in entries:
            child = Path(entry.path)
            relative = child.relative_to(root).as_posix()
            kind = _path_kind(child)
            if kind == "dir":
                fingerprint[relative] = "dir"
                _visit_tree(root, child, fingerprint)
            elif kind == "file":
                fingerprint[relative] = "file:" + _file_hash(child)
            elif kind == "link":
                fingerprint[relative] = "link:" + os.readlink(child)
            else:
                fingerprint[relative] = kind


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_path(path: Path) -> None:
    kind = _path_kind(path)
    if kind == "absent":
        return
    if kind == "dir":
        shutil.rmtree(path)
    else:
        path.unlink()
