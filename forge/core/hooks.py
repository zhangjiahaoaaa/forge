"""Lifecycle hook registration and execution helpers."""

from datetime import datetime


HOOK_NAMES = (
    "before_model",
    "after_model",
    "before_tool",
    "after_tool",
    "on_error",
    "on_final",
    "on_checkpoint",
)


def _callback_name(callback):
    return getattr(callback, "__name__", callback.__class__.__name__)


def _clip_text(text, limit=300):
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[: max(limit - 3, 0)] + "..."


class HookManager:
    def __init__(self):
        self._hooks = {name: [] for name in HOOK_NAMES}
        self._failures = []

    def register(self, name, callback):
        if name not in self._hooks:
            raise ValueError(f"unknown hook: {name}")
        if not callable(callback):
            raise TypeError("hook callback must be callable")
        self._hooks[name].append(callback)
        return callback

    def registered_summary(self):
        return {
            name: len(callbacks)
            for name, callbacks in self._hooks.items()
            if callbacks
        }

    def failures(self, run_id=None):
        if run_id is None:
            return [dict(item) for item in self._failures]
        return [
            dict(item) for item in self._failures if item.get("run_id", "") == str(run_id)
        ]

    def run(self, name, context=None):
        if name not in self._hooks:
            raise ValueError(f"unknown hook: {name}")
        payload = dict(context or {})
        payload["hook_name"] = name
        for callback in list(self._hooks[name]):
            try:
                callback(payload)
            except Exception as exc:
                self._record_failure(name, callback, payload, exc)
        return payload

    def _record_failure(self, name, callback, payload, exc):
        task_state = payload.get("task_state")
        run_id = str(
            payload.get("run_id", "")
            or getattr(task_state, "run_id", "")
            or ""
        )
        failure = {
            "hook_name": name,
            "callback": _callback_name(callback),
            "error": _clip_text(exc),
            "error_type": type(exc).__name__,
            "run_id": run_id,
            "task_id": str(
                payload.get("task_id", "")
                or getattr(task_state, "task_id", "")
                or ""
            ),
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        self._failures.append(failure)
        if task_state is not None and hasattr(task_state, "hook_failures"):
            task_state.hook_failures.append(dict(failure))
        runtime = payload.get("runtime")
        if runtime is None:
            return
        try:
            runtime.session_event_bus.emit(
                "hook_failed",
                {
                    "run_id": run_id,
                    "task_id": failure["task_id"],
                    "hook_name": name,
                    "callback": failure["callback"],
                    "error_type": failure["error_type"],
                    "error": failure["error"],
                },
            )
        except Exception:
            pass
        if task_state is None:
            return
        try:
            runtime.emit_trace(
                task_state,
                "hook_failed",
                {
                    "hook_name": name,
                    "callback": failure["callback"],
                    "error_type": failure["error_type"],
                    "error": failure["error"],
                },
            )
        except Exception:
            pass
