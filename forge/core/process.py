"""进程存活 / 创建时间探测（跨平台）。

Run 所有权协议依赖它：
- ``pid_alive(pid, started_at)``：进程是否存活；提供 started_at 时可排除
  PID 复用误判（原进程死后 PID 被系统分给无关进程）。
- ``process_started_at(pid)``：进程创建时间（UTC ISO）。

Windows 用 ``OpenProcess`` + ``GetProcessTimes``，不用 ``os.kill(pid, 0)``——
在本项目的嵌入式 Python 环境下，``os.kill(pid, 0)`` 会破坏子进程句柄状态，
导致后续 wait/poll 崩溃（实测）。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone


def _windows_pid_exists(pid: int) -> bool:
    """Windows：进程是否存在（OpenProcess 探测）。"""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        # 87 = ERROR_INVALID_PARAMETER → 进程不存在；其余（如拒绝访问）无法判断
        return kernel32.GetLastError() != 87
    kernel32.CloseHandle(handle)
    return True


def _windows_creation_time(pid: int) -> str | None:
    """Windows：返回进程创建时间（UTC ISO）；探测失败返回 None。"""
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        creation = wintypes.FILETIME()
        exit_t = wintypes.FILETIME()
        kernel_t = wintypes.FILETIME()
        user_t = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_t),
            ctypes.byref(kernel_t),
            ctypes.byref(user_t),
        ):
            return None
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return (datetime(1601, 1, 1, tzinfo=timezone.utc)
                + timedelta(microseconds=ticks // 10)).isoformat()
    finally:
        kernel32.CloseHandle(handle)


def _posix_started_at(pid: int) -> str | None:
    """POSIX（Linux）：/proc/<pid>/stat 的 starttime + btime → UTC ISO。"""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            after_comm = f.read().rsplit(")", 1)[1].split()
        start_ticks = int(after_comm[19])  # 字段 22（0-based 在 comm 之后）
        btime = 0
        with open("/proc/stat", encoding="utf-8") as f:
            for line in f:
                if line.startswith("btime "):
                    btime = int(line.split()[1])
                    break
        hz = os.sysconf("SC_CLK_TCK")
        return datetime.fromtimestamp(btime + start_ticks / hz, tz=timezone.utc).isoformat()
    except Exception:
        return None


def process_started_at(pid: int) -> str:
    """返回进程创建时间（UTC ISO）；无法获取返回空字符串。"""
    if not pid or pid <= 0:
        return ""
    if os.name == "nt":
        return _windows_creation_time(pid) or ""
    started = _posix_started_at(pid)
    return started or ""


def pid_alive(pid: int, started_at: str = "") -> bool:
    """判断进程是否存活；无法判断时按存活处理（fail closed，不接管）。

    ``started_at`` 提供时：若进程存在但创建时间与记录不一致，说明 PID 已被
    复用（原 owner 已死）→ 返回 False（可接管）。

    Windows 语义注意：进程退出后若仍有人持有其句柄（Popen / multiprocessing
    对象），``OpenProcess`` 依然能打开——因此必须用 ``GetExitCodeProcess``
    检查 ``STILL_ACTIVE``，而不能只看句柄能否打开。
    """
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        return _windows_alive(int(pid), started_at)
    # POSIX
    current = _posix_started_at(pid)
    if current is None and not _pid_exists_posix(pid):
        return False
    if started_at and current and current != started_at:
        return False  # PID 被复用
    return True


def _windows_alive(pid: int, started_at: str) -> bool:
    """Windows：OpenProcess + GetExitCodeProcess 判定进程存活。"""
    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # 87 = ERROR_INVALID_PARAMETER → 进程不存在；其余（如拒绝访问）无法判断
        return kernel32.GetLastError() != 87
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # 查询失败 → fail closed
        if code.value != STILL_ACTIVE:
            return False  # 已退出（即使句柄仍被其他对象持有）
        if started_at:
            creation = _windows_creation_time(pid)
            if creation and creation != started_at:
                return False  # PID 被复用 → 原 owner 已死
        return True
    finally:
        kernel32.CloseHandle(handle)


def _pid_exists_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return True  # 无法判断 → 视为存活
